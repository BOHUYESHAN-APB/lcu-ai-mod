import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.agent_state import AgentStateDB
from agent.skill_registry import SkillRegistry
from agent.task_coordinator import TaskCoordinator
from agent.task_preset_registry import TaskPresetRegistry
from protocol import BodyEvent


class FakeBody:
    def __init__(self):
        self.is_connected = True
        self.commands = []

    def connect(self):
        self.is_connected = True
        return True

    def disconnect(self):
        self.is_connected = False

    def send_command(self, command, args=None, request_id=None):
        self.commands.append((command, args or {}))
        return request_id or f"req-{len(self.commands)}"

    def drain(self):
        return []


class TaskCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = AgentStateDB(Path(self.tmp.name) / "agent_state.db")
        self.state.set_scheduler_clock(None, None, "default")
        self.registry = SkillRegistry()
        self.body = FakeBody()
        self.coordinator = TaskCoordinator(self.state, self.registry, self.body)
        self.coordinator.set_body_armed(True)
        self.presets = TaskPresetRegistry(self.registry)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    @staticmethod
    def crop(x, y, z, distance, token, age=7):
        return {
            "x": x, "y": y, "z": z, "distance": distance,
            "block_id": "minecraft:wheat", "target_token": token,
            "crop": {"mature": True, "age": age},
        }

    def send_crop_scan(self, parent, crops):
        request_id = self.state.get_run(parent["id"])["pending_request_id"]
        return self.coordinator.handle_body_message(BodyEvent("response", {
            "id": request_id, "success": True, "data": {"crops": crops},
        }))

    def test_response_progress_and_outcome_produce_durable_run_events(self):
        run = self.coordinator.create_run(
            "general.craft_item", {"item": "minecraft:torch", "count": 4}
        )

        self.assertEqual(run["status"], "dispatched")
        self.coordinator.handle_body_message(BodyEvent("response", {
            "id": run["request_id"], "success": True, "data": {"message": "queued"},
        }))
        self.assertEqual(self.state.get_run(run["id"])["status"], "running")

        self.coordinator.handle_body_message(BodyEvent("progress", {
            "id": run["request_id"], "progress": 1.0, "message": "crafted",
        }))
        self.assertEqual(self.state.get_run(run["id"])["status"], "running")
        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": run["request_id"], "status": "succeeded", "code": "CRAFTED", "message": "crafted",
        }))

        restored = self.state.get_run(run["id"])
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(restored["progress"], 1.0)
        event_types = [event["type"] for event in self.state.list_events()]
        self.assertEqual(event_types[-5:], ["run.created", "run.dispatched", "run.started", "run.progress", "run.succeeded"])

    def test_internal_session_command_uses_coordinator_raw_tracking(self):
        request_id = self.coordinator.dispatch_internal_command("jump", {}, "mode")

        self.assertEqual(self.body.commands, [("jump", {})])
        self.assertTrue(self.coordinator.is_busy())
        self.assertEqual(self.coordinator.get_status()["raw_request_count"], 1)

        self.coordinator.handle_body_message(BodyEvent("response", {
            "id": request_id, "success": True, "data": {},
        }))
        self.assertFalse(self.coordinator.is_busy())
        self.assertEqual(self.coordinator.get_status()["raw_request_count"], 0)

    def test_internal_body_action_is_rejected_while_external_lease_exists(self):
        self.state.acquire_lease("controller", "external", ["actions"], 30)

        with self.assertRaisesRegex(RuntimeError, "external control lease"):
            self.coordinator.dispatch_internal_command("jump", {}, "mode")

        self.assertEqual(self.body.commands, [])

    def test_explicit_outcome_produces_terminal_run(self):
        run = self.coordinator.create_run(
            "general.craft_item", {"item": "minecraft:torch", "count": 4}
        )

        self.coordinator.handle_body_message(BodyEvent("response", {
            "id": run["request_id"], "success": True, "data": {"message": "queued"},
        }))
        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": run["request_id"], "status": "failed", "code": "NO_SOURCE",
            "message": "no collectible source",
        }))

        restored = self.state.get_run(run["id"])
        self.assertEqual(restored["status"], "failed")
        self.assertEqual(restored["error"], "NO_SOURCE")

    def test_late_duplicate_outcome_cannot_replace_terminal_result(self):
        run = self.coordinator.create_run(
            "general.craft_item", {"item": "minecraft:torch", "count": 1}
        )
        self.coordinator.handle_body_message(BodyEvent("response", {
            "id": run["request_id"], "success": True, "data": {},
        }))
        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": run["request_id"], "status": "succeeded", "code": "CRAFTED", "message": "done",
        }))
        event_count = len(self.state.list_events())

        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": run["request_id"], "status": "failed", "code": "LATE", "message": "late",
        }))

        restored = self.state.get_run(run["id"])
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(len(self.state.list_events()), event_count)

    def test_cancel_uses_operation_id_and_waits_for_outcome(self):
        run = self.coordinator.create_run("core.move_to", {"x": 1, "y": 64, "z": 2})

        self.coordinator.handle_body_message(BodyEvent("response", {
            "id": run["request_id"], "success": True, "data": {"message": "accepted"},
        }))
        cancelled = self.coordinator.cancel(run["id"])

        self.assertEqual(cancelled["status"], "running")
        self.assertEqual(self.body.commands[-1][0], "cancel_operation")
        self.assertEqual(self.body.commands[-1][1]["operation_id"], run["request_id"])

        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": run["request_id"], "status": "cancelled", "code": "CANCELLED",
            "message": "cancelled",
        }))
        self.assertEqual(self.state.get_run(run["id"])["status"], "cancelled")

    def test_disconnect_marks_inflight_run_unknown_without_replay(self):
        run = self.coordinator.create_run("core.move_to", {"x": 1, "y": 64, "z": 2})

        changed = self.coordinator.on_disconnect()

        self.assertEqual(changed, 1)
        self.assertEqual(self.state.get_run(run["id"])["status"], "unknown")
        self.assertEqual(len(self.body.commands), 1)
        queued = self.coordinator.create_run("core.jump", {})
        self.assertEqual(queued["status"], "queued")

    def test_restart_marks_persisted_inflight_run_unknown(self):
        run = self.coordinator.create_run("core.move_to", {"x": 1, "y": 64, "z": 2})

        TaskCoordinator(self.state, self.registry, self.body)

        self.assertEqual(self.state.get_run(run["id"])["status"], "unknown")

    def test_second_run_stays_queued_until_first_is_terminal(self):
        first = self.coordinator.create_run("core.move_to", {"x": 1, "y": 64, "z": 2})
        second = self.coordinator.create_run("core.jump", {})

        self.assertEqual(first["status"], "dispatched")
        self.assertEqual(second["status"], "queued")
        self.assertEqual(len(self.body.commands), 1)

    def test_automatic_admission_rejects_busy_body_without_creating_queued_run(self):
        active = self.coordinator.create_run("core.move_to", {"x": 1, "y": 64, "z": 2})

        with self.assertRaisesRegex(ValueError, "already executing"):
            self.coordinator.admit_automatic_run("general.eat", {})

        roots = self.state.list_runs(root_only=True)
        self.assertEqual([run["id"] for run in roots], [active["id"]])

    def test_repeated_stop_requests_are_coalesced_until_body_response(self):
        first = self.coordinator.preempt_all("first")
        second = self.coordinator.preempt_all("second")

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.body.commands, [("stop_all", {})])
        self.coordinator.handle_body_message(BodyEvent("response", {
            "id": first["id"], "success": True, "data": {"message": "stopped"},
        }))
        third = self.coordinator.preempt_all("third")
        self.assertNotEqual(third["id"], first["id"])

    def test_uncertain_planner_dispatch_is_not_reported_as_admitted(self):
        with patch.object(self.body, "send_command", side_effect=ConnectionError("offline")):
            with self.assertRaisesRegex(ConnectionError, "uncertain"):
                self.coordinator.admit_planner_run("general.eat", {})

        run = self.state.list_runs(root_only=True)[0]
        self.assertEqual(run["status"], "unknown")

    def test_workflow_dispatches_steps_in_order_and_completes_parent(self):
        workflow = self.presets.render("workflow.starter_chest", {})
        parent = self.coordinator.create_workflow(workflow)
        first_id = parent["active_child_id"]

        self.assertEqual(parent["status"], "running")
        self.assertEqual(self.body.commands[0], (
            "collect_blocks", {"block_type": "#minecraft:logs", "count": 8},
        ))
        self.coordinator.handle_body_message(BodyEvent("response", {
            "id": first_id, "success": True, "data": {"message": "accepted"},
        }))
        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": first_id, "status": "succeeded", "message": "collected",
        }))

        parent = self.state.get_run(parent["id"])
        second_id = parent["active_child_id"]
        self.assertEqual(parent["current_step"], 1)
        self.assertEqual(self.body.commands[1], (
            "craft_item", {"item": "minecraft:chest", "count": 1},
        ))
        self.coordinator.handle_body_message(BodyEvent("response", {
            "id": second_id, "success": True, "data": {"message": "accepted"},
        }))
        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": second_id, "status": "succeeded", "message": "crafted",
        }))

        restored = self.state.get_run(parent["id"])
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual([step["status"] for step in restored["steps"]], ["succeeded", "succeeded"])

    def test_farm_region_sorts_mature_candidates_and_harvests_serially(self):
        parent = self.coordinator.create_workflow(self.presets.render("farm.region", {"radius": 8}))
        self.assertEqual(self.body.commands, [("scan_crops", {"radius": 8})])

        self.send_crop_scan(parent, [
            self.crop(5, 64, 0, 5.0, "far"),
            self.crop(2, 64, 0, 2.0, "near-b"),
            self.crop(1, 64, 0, 2.0, "near-a"),
            {**self.crop(0, 64, 0, 1.0, "young", age=3), "crop": {"mature": False, "age": 3}},
        ])

        self.assertEqual(len(self.body.commands), 2)
        self.assertEqual(self.body.commands[-1], ("harvest_crop_at", {
            "x": 1, "y": 64, "z": 0, "block_id": "minecraft:wheat",
            "age": 7, "target_token": "near-a",
        }))
        first_child = self.state.get_run(parent["id"])["active_child_id"]
        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": first_child, "status": "succeeded", "code": "HARVESTED_AND_REPLANTED",
            "message": "done",
        }))
        self.assertEqual(len(self.body.commands), 3)
        self.assertEqual(self.body.commands[-1][1]["target_token"], "near-b")

        second_child = self.state.get_run(parent["id"])["active_child_id"]
        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": second_child, "status": "succeeded", "message": "done",
        }))
        third_child = self.state.get_run(parent["id"])["active_child_id"]
        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": third_child, "status": "succeeded", "message": "done",
        }))

        restored = self.state.get_run(parent["id"])
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(restored["result"]["harvested"], 3)
        self.assertEqual(restored["result"]["candidate_attempts"], 3)

    def test_farm_region_records_restoration_obligation_and_continues(self):
        parent = self.coordinator.create_workflow(self.presets.render("farm.region", {"radius": 4}))
        self.send_crop_scan(parent, [
            self.crop(1, 64, 0, 1.0, "first"), self.crop(2, 64, 0, 2.0, "second"),
        ])
        first_child = self.state.get_run(parent["id"])["active_child_id"]

        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": first_child, "status": "failed", "code": "SEED_MISSING",
            "message": "harvested but no seed",
        }))

        second_child = self.state.get_run(parent["id"])["active_child_id"]
        self.assertNotEqual(second_child, first_child)
        self.assertEqual(self.body.commands[-1][1]["target_token"], "second")
        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": second_child, "status": "succeeded", "message": "done",
        }))
        restored = self.state.get_run(parent["id"])
        self.assertEqual(restored["status"], "failed")
        self.assertEqual(restored["result"]["status"], "partial")
        self.assertEqual(restored["result"]["harvested"], 2)
        self.assertEqual(restored["result"]["restoration_obligations"][0]["code"], "SEED_MISSING")

    def test_farm_region_bounds_rescans_after_stale_candidates(self):
        parent = self.coordinator.create_workflow(self.presets.render("farm.region", {"radius": 4}))
        for attempt in range(3):
            self.send_crop_scan(parent, [self.crop(1, 64, attempt, 1.0, f"token-{attempt}")])
            child_id = self.state.get_run(parent["id"])["active_child_id"]
            self.coordinator.handle_body_message(BodyEvent("outcome", {
                "id": child_id, "status": "failed", "code": "STALE_TARGET", "message": "stale",
            }))

        restored = self.state.get_run(parent["id"])
        self.assertEqual(restored["status"], "failed")
        self.assertEqual(restored["result"]["scan_attempts"], 3)
        self.assertEqual([command for command, _ in self.body.commands].count("scan_crops"), 3)

    def test_farm_scan_can_be_resent_after_restart(self):
        parent = self.coordinator.create_workflow(self.presets.render("farm.region", {"radius": 6}))

        restarted = TaskCoordinator(self.state, self.registry, self.body)
        restarted.set_body_armed(True)
        restored = self.state.get_run(parent["id"])
        self.assertEqual(restored["status"], "queued")
        self.assertIsNone(restored["pending_request_id"])

        restarted.resume(parent["id"])
        self.assertEqual([command for command, _ in self.body.commands].count("scan_crops"), 2)

    def test_uncertain_farm_harvest_is_not_replayed_after_restart(self):
        parent = self.coordinator.create_workflow(self.presets.render("farm.region", {"radius": 4}))
        self.send_crop_scan(parent, [self.crop(1, 64, 0, 1.0, "token")])
        child_id = self.state.get_run(parent["id"])["active_child_id"]
        command_count = len(self.body.commands)

        TaskCoordinator(self.state, self.registry, self.body)

        restored = self.state.get_run(parent["id"])
        self.assertEqual(self.state.get_run(child_id)["status"], "unknown")
        self.assertEqual(restored["status"], "unknown")
        self.assertEqual(restored["result"]["status"], "unknown")
        self.assertEqual(len(self.body.commands), command_count)

    def test_uncertain_farm_harvest_after_progress_is_partial_unknown(self):
        parent = self.coordinator.create_workflow(self.presets.render("farm.region", {"radius": 4}))
        self.send_crop_scan(parent, [
            self.crop(1, 64, 0, 1.0, "first"), self.crop(2, 64, 0, 2.0, "second"),
        ])
        first_child = self.state.get_run(parent["id"])["active_child_id"]
        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": first_child, "status": "succeeded", "message": "done",
        }))
        second_child = self.state.get_run(parent["id"])["active_child_id"]

        TaskCoordinator(self.state, self.registry, self.body)

        restored = self.state.get_run(parent["id"])
        self.assertEqual(self.state.get_run(second_child)["status"], "unknown")
        self.assertEqual(restored["status"], "unknown")
        self.assertEqual(restored["result"]["status"], "partial_unknown")
        self.assertEqual(restored["result"]["harvested"], 1)

    def test_workflow_preserves_external_lease_fencing_across_steps(self):
        lease = self.state.acquire_lease("controller", "external", ["actions"], 30)
        parent = self.coordinator.create_workflow(
            self.presets.render("workflow.starter_chest", {}),
            lease_id=lease["id"], fencing_token=lease["fencing_token"],
        )
        first_id = parent["active_child_id"]
        self.assertEqual(self.body.commands[0][1]["__lcu_fencing_token"], lease["fencing_token"])

        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": first_id, "status": "succeeded", "message": "collected",
        }))

        self.assertEqual(self.body.commands[1][0], "craft_item")
        self.assertEqual(self.body.commands[1][1]["__lcu_fencing_token"], lease["fencing_token"])

    def test_workflow_cancel_targets_active_child_and_propagates(self):
        parent = self.coordinator.create_workflow(self.presets.render("workflow.starter_chest", {}))
        child_id = parent["active_child_id"]
        self.coordinator.handle_body_message(BodyEvent("response", {
            "id": child_id, "success": True, "data": {"message": "accepted"},
        }))

        cancelled = self.coordinator.cancel(parent["id"])

        self.assertEqual(cancelled["status"], "running")
        self.assertEqual(self.body.commands[-1][0], "cancel_operation")
        self.assertEqual(self.body.commands[-1][1]["operation_id"], child_id)
        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": child_id, "status": "cancelled", "message": "cancelled",
        }))
        self.assertEqual(self.state.get_run(parent["id"])["status"], "cancelled")

    def test_restart_marks_inflight_workflow_and_child_unknown(self):
        parent = self.coordinator.create_workflow(self.presets.render("workflow.starter_chest", {}))
        child_id = parent["active_child_id"]

        TaskCoordinator(self.state, self.registry, self.body)

        self.assertEqual(self.state.get_run(child_id)["status"], "unknown")
        self.assertEqual(self.state.get_run(parent["id"])["status"], "unknown")

    def test_process_restart_reopens_database_and_preserves_queued_workflow(self):
        self.coordinator.set_body_armed(False)
        parent = self.coordinator.create_workflow(self.presets.render("workflow.starter_chest", {}))
        path = self.state.path
        self.state.close()

        self.state = AgentStateDB(path)
        coordinator = TaskCoordinator(self.state, self.registry, self.body)
        coordinator.set_body_armed(True)
        resumed = coordinator.resume(parent["id"])

        self.assertEqual(resumed["status"], "running")
        self.assertEqual(self.body.commands[-1][0], "collect_blocks")

    def test_process_restart_reopens_database_and_marks_inflight_workflow_unknown(self):
        parent = self.coordinator.create_workflow(self.presets.render("workflow.starter_chest", {}))
        child_id = parent["active_child_id"]
        path = self.state.path
        self.state.close()

        self.state = AgentStateDB(path)
        TaskCoordinator(self.state, self.registry, self.body)

        self.assertEqual(self.state.get_run(child_id)["status"], "unknown")
        self.assertEqual(self.state.get_run(parent["id"])["status"], "unknown")

    def test_queued_workflow_resumes_from_parent(self):
        self.coordinator.set_body_armed(False)
        parent = self.coordinator.create_workflow(self.presets.render("workflow.starter_chest", {}))
        self.assertEqual(parent["status"], "queued")

        self.coordinator.set_body_armed(True)
        resumed = self.coordinator.resume(parent["id"])

        self.assertEqual(resumed["status"], "running")
        self.assertEqual(self.body.commands[-1][0], "collect_blocks")

    def test_cancel_requested_queued_workflow_is_finalized_instead_of_resumed(self):
        self.coordinator.set_body_armed(False)
        parent = self.coordinator.create_workflow(self.presets.render("workflow.starter_chest", {}))
        self.state.request_workflow_cancel(parent["id"])
        self.body.is_connected = False

        resumed = self.coordinator.resume(parent["id"])

        self.assertEqual(resumed["status"], "cancelled")
        self.assertEqual(self.body.commands, [])

    def test_queued_run_can_be_cancelled_after_skill_is_removed(self):
        run = self.state.create_run(
            {"id": "removed.skill", "version": "1.0.0", "completion": "outcome"},
            {}, scope_id="default",
        )

        cancelled = self.coordinator.cancel(run["id"])

        self.assertEqual(cancelled["status"], "cancelled")

    def test_initial_scope_allows_fresh_database_workflow_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "fresh.db")
            coordinator = TaskCoordinator(state, self.registry, self.body, initial_scope="server\0world")
            coordinator.set_body_armed(False)
            parent = coordinator.create_workflow(self.presets.render("workflow.starter_chest", {}))
            coordinator.set_body_armed(True)

            resumed = coordinator.resume(parent["id"])

            self.assertEqual(parent["scope_id"], "server\0world")
            self.assertEqual(resumed["status"], "running")
            state.close()

    def test_initial_scope_replaces_persisted_scope_before_first_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "fresh.db")
            state.set_scheduler_clock(1200, 600, "old\0world")
            coordinator = TaskCoordinator(state, self.registry, self.body, initial_scope="new\0world")
            coordinator.set_body_armed(False)

            parent = coordinator.create_workflow(self.presets.render("workflow.starter_chest", {}))

            self.assertEqual(parent["scope_id"], "new\0world")
            self.assertEqual(state.get_scheduler_clock(), {
                "game_time": None, "day_time": None, "scope_id": "new\0world",
            })
            state.close()

    def test_raw_outcome_command_blocks_durable_dispatch_until_outcome(self):
        with self.coordinator.raw_command_guard():
            request_id = self.coordinator.dispatch_raw_command(
                "craft_item", {"item": "minecraft:torch", "count": 4},
            )
        run = self.coordinator.create_run("core.jump", {})

        self.assertTrue(self.coordinator.is_busy())
        self.assertEqual(run["status"], "queued")
        self.coordinator.handle_body_message(BodyEvent("response", {
            "id": request_id, "success": True, "data": {"message": "queued"},
        }))
        self.assertTrue(self.coordinator.is_busy())

        self.coordinator.handle_body_message(BodyEvent("progress", {
            "id": request_id, "progress": 1.0, "message": "crafted",
        }))
        self.assertTrue(self.coordinator.is_busy())
        self.coordinator.handle_body_message(BodyEvent("outcome", {
            "id": request_id, "status": "succeeded", "message": "crafted",
        }))
        self.coordinator.resume(run["id"])

        self.assertEqual(self.state.get_run(run["id"])["status"], "dispatched")
        self.assertEqual([command for command, _ in self.body.commands], ["craft_item", "jump"])

    def test_reconnect_does_not_implicitly_resume_queued_run(self):
        self.coordinator.set_body_armed(False)
        run = self.coordinator.create_run("core.jump", {})
        self.coordinator.set_body_armed(True)

        with patch("agent.task_coordinator.time.monotonic", return_value=10.0):
            self.coordinator.tick({"world": {}}, "builtin")

        self.assertEqual(self.state.get_run(run["id"])["status"], "queued")
        self.assertEqual(self.body.commands, [])
        self.coordinator.resume(run["id"])
        self.assertEqual(self.state.get_run(run["id"])["status"], "dispatched")

    def test_raw_command_guard_rejects_concurrent_command(self):
        with self.coordinator.raw_command_guard():
            self.coordinator.dispatch_raw_command("jump", {})

        with self.assertRaisesRegex(ValueError, "already executing"):
            with self.coordinator.raw_command_guard():
                pass

    def test_failed_raw_send_releases_admission_and_pending_registration(self):
        registered = []
        removed = []
        with patch.object(self.body, "send_command", side_effect=ConnectionError("offline")):
            with self.assertRaisesRegex(ConnectionError, "offline"):
                with self.coordinator.raw_command_guard():
                    self.coordinator.dispatch_raw_command(
                        "jump", {}, on_reserved=registered.append, on_failed=removed.append,
                    )

        self.assertFalse(self.coordinator.is_busy())
        self.assertEqual(removed, registered)

    def test_wall_interval_schedule_creates_and_dispatches_run(self):
        manifest = self.registry.get("core.jump")
        schedule = self.state.create_schedule({
            "name": "jump check",
            "skill_id": manifest.id,
            "skill_version": manifest.version,
            "input": {},
            "enabled": True,
            "clock": "wall",
            "trigger_type": "interval",
            "misfire_policy": "fire_once",
            "wall_run_at": 100.0,
            "wall_interval_seconds": 60.0,
            "game_interval_ticks": None,
            "time_of_day_tick": None,
            "next_wall_at": 100.0,
            "next_game_tick": None,
        })
        self.coordinator._last_scheduler_tick = 0.0

        with patch("agent.task_coordinator.time.monotonic", return_value=10.0), \
                patch("agent.task_coordinator.time.time", return_value=100.0):
            self.coordinator.tick({"world": {}}, "builtin")

        runs = self.state.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["schedule_id"], schedule["id"])
        self.assertEqual(runs[0]["status"], "dispatched")
        self.assertEqual(self.state.get_schedule(schedule["id"])["next_wall_at"], 160.0)

    def test_schedule_from_another_scope_does_not_fire(self):
        manifest = self.registry.get("core.jump")
        self.state.create_schedule({
            "name": "other world", "skill_id": manifest.id, "skill_version": manifest.version,
            "input": {}, "enabled": True, "clock": "wall", "trigger_type": "interval",
            "misfire_policy": "fire_once", "wall_run_at": 100.0, "wall_interval_seconds": 60.0,
            "game_interval_ticks": None, "time_of_day_tick": None,
            "next_wall_at": 100.0, "next_game_tick": None, "scope_id": "other",
        })

        with patch("agent.task_coordinator.time.monotonic", return_value=10.0), \
                patch("agent.task_coordinator.time.time", return_value=100.0):
            self.coordinator.tick({"world": {}}, "builtin", "default")

        self.assertEqual(self.state.list_runs(), [])

    def test_game_time_of_day_schedule_initializes_then_fires_once(self):
        manifest = self.registry.get("core.jump")
        schedule = self.state.create_schedule({
            "name": "morning jump",
            "skill_id": manifest.id,
            "skill_version": manifest.version,
            "input": {},
            "enabled": True,
            "clock": "game",
            "trigger_type": "time_of_day",
            "misfire_policy": "fire_once",
            "wall_run_at": None,
            "wall_interval_seconds": None,
            "game_interval_ticks": None,
            "time_of_day_tick": 1000,
            "next_wall_at": None,
            "next_game_tick": None,
        })

        with patch("agent.task_coordinator.time.monotonic", side_effect=[10.0, 11.0]):
            self.coordinator.tick({"world": {"game_time": 500, "day_time": 500}}, "builtin")
            self.coordinator.tick({"world": {"game_time": 1000, "day_time": 1000}}, "builtin")

        self.assertEqual(len(self.state.list_runs()), 1)
        self.assertEqual(self.state.get_schedule(schedule["id"])["next_game_tick"], 25000)

    def test_skip_schedule_advances_while_body_is_disconnected(self):
        manifest = self.registry.get("core.jump")
        schedule = self.state.create_schedule({
            "name": "skip offline",
            "skill_id": manifest.id,
            "skill_version": manifest.version,
            "input": {}, "enabled": True, "clock": "wall", "trigger_type": "interval",
            "misfire_policy": "skip", "wall_run_at": 100.0, "wall_interval_seconds": 60.0,
            "game_interval_ticks": None, "time_of_day_tick": None,
            "next_wall_at": 100.0, "next_game_tick": None,
        })
        self.body.is_connected = False

        with patch("agent.task_coordinator.time.monotonic", return_value=10.0), \
                patch("agent.task_coordinator.time.time", return_value=130.0):
            self.coordinator.tick({"world": {}}, "builtin")

        self.assertEqual(self.state.list_runs(), [])
        self.assertEqual(self.state.get_schedule(schedule["id"])["next_wall_at"], 160.0)
        self.assertIn("schedule.misfired", [event["type"] for event in self.state.list_events()])

    def test_skipped_one_shot_is_disabled_atomically(self):
        manifest = self.registry.get("core.jump")
        schedule = self.state.create_schedule({
            "name": "missed once", "skill_id": manifest.id, "skill_version": manifest.version,
            "input": {}, "enabled": True, "clock": "wall", "trigger_type": "once",
            "misfire_policy": "skip", "wall_run_at": 100.0, "wall_interval_seconds": None,
            "game_interval_ticks": None, "time_of_day_tick": None,
            "next_wall_at": 100.0, "next_game_tick": None,
        })
        self.body.is_connected = False

        with patch("agent.task_coordinator.time.monotonic", return_value=10.0), \
                patch("agent.task_coordinator.time.time", return_value=130.0):
            self.coordinator.tick({"world": {}}, "builtin")

        restored = self.state.get_schedule(schedule["id"])
        self.assertFalse(restored["enabled"])
        self.assertIsNone(restored["next_wall_at"])


if __name__ == "__main__":
    unittest.main()
