import unittest
from concurrent.futures import Future

from agent.decision_scheduler import DecisionScheduler


class ImmediateFuture:
    def __init__(self, fn, *args, **kwargs):
        try:
            self.value = fn(*args, **kwargs)
            self.error = None
        except Exception as exc:
            self.value = None
            self.error = exc

    def done(self):
        return True

    def result(self):
        if self.error:
            raise self.error
        return self.value


class ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        return ImmediateFuture(fn, *args, **kwargs)


class ManualExecutor:
    def __init__(self):
        self.future = Future()
        self.call = None

    def submit(self, fn, *args, **kwargs):
        self.call = (fn, args, kwargs)
        return self.future

    def complete(self):
        fn, args, kwargs = self.call
        self.future.set_result(fn(*args, **kwargs))


class FakeLLM:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def chat(self, messages, agent=None, **kwargs):
        self.calls.append((messages, agent, kwargs))
        return {"content": self.content}


class DecisionSchedulerTests(unittest.TestCase):
    def test_async_request_becomes_typed_proposal_and_resolves(self):
        llm = FakeLLM('{"decision":"run_skill","skill_id":"general.eat","input":{},"reason":"low hunger"}')
        scheduler = DecisionScheduler(llm, executor=ImmediateExecutor())
        triggers = [{"sequence": 4, "type": "player.hunger_low"}]

        accepted = scheduler.submit(
            triggers, {"player": {"hunger": 5}}, scope_id="server\0world",
            body_epoch=2, observation_revision=9, submitted_at=10,
        )
        result = scheduler.poll()

        self.assertTrue(accepted)
        self.assertEqual(result.proposal.skill_id, "general.eat")
        self.assertEqual(result.through_sequence, 4)
        self.assertEqual(llm.calls[0][1], "decision_scheduler")
        record = scheduler.resolve("dispatched", run_id="run-1", resolved_at=12)
        self.assertEqual(record["run_id"], "run-1")
        self.assertEqual(scheduler.get_status()["state"], "idle")

    def test_invalid_model_output_is_captured_as_failed_result(self):
        scheduler = DecisionScheduler(FakeLLM("eat now"), executor=ImmediateExecutor())
        scheduler.submit(
            [{"sequence": 1}], {}, scope_id="server\0world", body_epoch=1,
            observation_revision=1, submitted_at=10,
        )

        result = scheduler.poll()

        self.assertIsNone(result.proposal)
        self.assertIn("valid JSON", result.error)

    def test_parser_accepts_fenced_json_and_rejects_untyped_input(self):
        proposal = DecisionScheduler.parse_proposal("""```json
{"decision":"none","reason":"stable"}
```""")
        self.assertEqual(proposal.decision, "none")
        with self.assertRaisesRegex(ValueError, "input must be an object"):
            DecisionScheduler.parse_proposal(
                '{"decision":"run_skill","skill_id":"general.eat","input":[]}'
            )

    def test_proposal_expiration_uses_completion_time(self):
        scheduler = DecisionScheduler(FakeLLM('{"decision":"none"}'), proposal_ttl_seconds=5, executor=ImmediateExecutor())
        scheduler.submit(
            [{"sequence": 1}], {}, scope_id="server\0world", body_epoch=1,
            observation_revision=1, submitted_at=10,
        )
        result = scheduler.poll()

        self.assertFalse(scheduler.is_expired(result, now=result.completed_at + 5))
        self.assertTrue(scheduler.is_expired(result, now=result.completed_at + 5.01))

    def test_transient_failure_retries_before_acknowledgement_limit(self):
        scheduler = DecisionScheduler(FakeLLM("invalid"), max_attempts=2, executor=ImmediateExecutor())
        args = {
            "triggers": [{"sequence": 7}], "context": {}, "scope_id": "server\0world",
            "body_epoch": 1, "observation_revision": 2,
        }
        scheduler.submit(**args, submitted_at=10)
        first = scheduler.poll()
        record = scheduler.resolve("failed", detail=first.error, resolved_at=11)

        self.assertTrue(record["retryable"])
        self.assertFalse(scheduler.submit(**args, submitted_at=12))
        self.assertTrue(scheduler.submit(**args, submitted_at=13.01))
        second = scheduler.poll()
        record = scheduler.resolve("failed", detail=second.error, resolved_at=14)
        self.assertFalse(record["retryable"])

    def test_close_is_idempotent_and_rejects_new_submissions(self):
        scheduler = DecisionScheduler(FakeLLM('{"decision":"none"}'))
        scheduler.close()
        scheduler.close()

        self.assertFalse(scheduler.submit(
            [{"sequence": 1}], {}, scope_id="server\0world", body_epoch=1,
            observation_revision=1,
        ))
        self.assertTrue(scheduler.get_status()["closed"])

    def test_invalidated_inflight_result_cannot_reappear(self):
        executor = ManualExecutor()
        scheduler = DecisionScheduler(FakeLLM('{"decision":"none"}'), executor=executor)
        scheduler.submit(
            [{"sequence": 3}], {}, scope_id="old\0world", body_epoch=1,
            observation_revision=7,
        )

        scheduler.invalidate("body disconnected")
        self.assertEqual(scheduler.get_status()["state"], "invalidated")
        self.assertFalse(scheduler.submit(
            [{"sequence": 4}], {}, scope_id="new\0world", body_epoch=2,
            observation_revision=8,
        ))
        executor.complete()

        self.assertIsNone(scheduler.poll())
        self.assertEqual(scheduler.get_status()["state"], "idle")
        self.assertEqual(scheduler.get_status()["history"][-1]["disposition"], "invalidated")

    def test_retry_batch_does_not_expand_when_new_triggers_arrive(self):
        scheduler = DecisionScheduler(FakeLLM("invalid"), max_attempts=3, executor=ImmediateExecutor())
        common = {"context": {}, "scope_id": "server\0world", "body_epoch": 1, "observation_revision": 2}
        scheduler.submit(triggers=[{"sequence": 7}], submitted_at=10, **common)
        first = scheduler.poll()
        scheduler.resolve("failed", detail=first.error, resolved_at=11)

        scheduler.submit(
            triggers=[{"sequence": 7}, {"sequence": 8}], submitted_at=13.01, **common,
        )
        second = scheduler.poll()

        self.assertEqual(second.through_sequence, 7)


if __name__ == "__main__":
    unittest.main()
