"""Apache-2.0 licensed integration client for the LCU companion backend."""

# Copyright 2026 LCU Mod Contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

import httpx


class LCUClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8080", timeout: float = 30.0,
                 api_token: str | None = None):
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_token}"} if api_token else None
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, headers=headers)

    def __enter__(self) -> "LCUClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _get(self, path: str) -> dict[str, Any]:
        response = self._client.get(path)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    def _patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.patch(path, json=payload)
        response.raise_for_status()
        return response.json()

    def _delete(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.request("DELETE", path, json=payload)
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()

    def get_status(self) -> dict[str, Any]:
        return self._get("/api/status")

    def get_sdk_info(self) -> dict[str, Any]:
        return self._get("/api/sdk/info")

    def get_v2_info(self) -> dict[str, Any]:
        return self._get("/api/v2/info")

    def get_session(self) -> dict[str, Any]:
        return self._get("/api/session")

    def get_memory(self) -> dict[str, Any]:
        return self._get("/api/memory")

    def get_identity(self) -> dict[str, Any]:
        return self._get("/api/sdk/identity").get("identity", {})

    def set_identity(self, **identity: Any) -> dict[str, Any]:
        return self._post("/api/sdk/identity", identity)

    def list_provider_presets(self) -> list[dict[str, Any]]:
        return self._get("/api/llm/providers").get("providers", [])

    def set_llm_config(self, agent: str = "default", **config: Any) -> dict[str, Any]:
        payload = {"agent": agent, **config}
        return self._post("/api/llm/config", payload)

    def fetch_models(self, agent: str = "default", **overrides: Any) -> list[str]:
        payload = {"agent": agent, **overrides}
        return self._post("/api/llm/models", payload).get("models", [])

    def get_persona(self) -> dict[str, Any]:
        return self._get("/api/persona")

    def set_persona(self, **persona: Any) -> dict[str, Any]:
        return self._post("/api/persona", persona)

    def get_external_context(self) -> dict[str, Any]:
        return self._get("/api/sdk/context")

    def push_external_context(self, external_context: dict[str, Any]) -> dict[str, Any]:
        return self._post("/api/sdk/context", {"external_context": external_context})

    def send_chat(self, message: str, sender: str = "sdk") -> str:
        result = self._post("/api/sdk/chat", {"message": message, "sender": sender})
        return str(result.get("response", ""))

    def send_command(self, command: str, args: dict[str, Any] | None = None) -> str:
        result = self._post("/api/sdk/command", {"command": command, "args": args or {}})
        return str(result["request_id"])

    def get_body_request(self, request_id: str) -> dict[str, Any]:
        return self._get(f"/api/v2/body-requests/{quote(request_id, safe='')}")

    def list_skills(self, category: str | None = None) -> list[dict[str, Any]]:
        suffix = f"?{urlencode({'category': category})}" if category else ""
        return self._get(f"/api/v2/skills{suffix}").get("skills", [])

    def get_skill(self, skill_id: str) -> dict[str, Any]:
        return self._get(f"/api/v2/skills/{quote(skill_id, safe='')}")

    def list_task_presets(self, category: str | None = None) -> list[dict[str, Any]]:
        suffix = f"?{urlencode({'category': category})}" if category else ""
        return self._get(f"/api/v2/task-presets{suffix}").get("presets", [])

    def get_task_preset(self, preset_id: str) -> dict[str, Any]:
        return self._get(f"/api/v2/task-presets/{quote(preset_id, safe='')}")

    def get_control(self) -> dict[str, Any]:
        return self._get("/api/v2/control")

    def create_player_pairing(self, player_id: str, server_id: str) -> dict[str, Any]:
        return self._post("/api/v2/player-pairings", {
            "player_id": player_id, "server_id": server_id,
        })

    def acquire_control(self, owner: str, mode: str = "external", *,
                        owns: list[str] | None = None, ttl_seconds: int = 30) -> dict[str, Any]:
        payload: dict[str, Any] = {"owner": owner, "mode": mode, "ttl_seconds": ttl_seconds}
        if owns is not None:
            payload["owns"] = owns
        return self._post("/api/v2/control/leases", payload)["lease"]

    def heartbeat_control(self, lease_id: str, fencing_token: int,
                          ttl_seconds: int = 30) -> dict[str, Any]:
        return self._post(
            f"/api/v2/control/leases/{lease_id}/heartbeat",
            {"fencing_token": fencing_token, "ttl_seconds": ttl_seconds},
        )["lease"]

    def release_control(self, lease_id: str, fencing_token: int) -> dict[str, Any]:
        return self._post(
            f"/api/v2/control/leases/{lease_id}/release",
            {"fencing_token": fencing_token},
        )["lease"]

    def run_skill(self, skill_id: str, input: dict[str, Any] | None = None, *,
                  lease_id: str | None = None, fencing_token: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"input": input or {}}
        if lease_id is not None:
            payload["lease_id"] = lease_id
        if fencing_token is not None:
            payload["fencing_token"] = fencing_token
        return self._post(f"/api/v2/skills/{quote(skill_id, safe='')}/runs", payload)

    def run_task_preset(self, preset_id: str, parameters: dict[str, Any] | None = None, *,
                        lease_id: str | None = None, fencing_token: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"parameters": parameters or {}}
        if lease_id is not None:
            payload["lease_id"] = lease_id
        if fencing_token is not None:
            payload["fencing_token"] = fencing_token
        return self._post(f"/api/v2/task-presets/{quote(preset_id, safe='')}/runs", payload)

    def list_runs(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"limit": limit}
        if status:
            query["status"] = status
        return self._get(f"/api/v2/runs?{urlencode(query)}").get("runs", [])

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._get(f"/api/v2/runs/{quote(run_id, safe='')}")

    def cancel_run(self, run_id: str, *, lease_id: str | None = None,
                   fencing_token: int | None = None) -> dict[str, Any]:
        payload = self._lease_payload(lease_id, fencing_token)
        return self._post(f"/api/v2/runs/{quote(run_id, safe='')}/cancel", payload)

    def resume_run(self, run_id: str, *, lease_id: str | None = None,
                   fencing_token: int | None = None) -> dict[str, Any]:
        payload = self._lease_payload(lease_id, fencing_token)
        return self._post(f"/api/v2/runs/{quote(run_id, safe='')}/resume", payload)

    def list_events(self, after: int = 0, limit: int = 100, latest: bool = False) -> dict[str, Any]:
        return self._get(f"/api/v2/events?{urlencode({'after': after, 'limit': limit, 'latest': str(latest).lower()})}")

    def list_schedules(self) -> list[dict[str, Any]]:
        return self._get("/api/v2/schedules").get("schedules", [])

    def create_schedule(self, schedule: dict[str, Any], *, lease_id: str | None = None,
                        fencing_token: int | None = None) -> dict[str, Any]:
        return self._post("/api/v2/schedules", {
            **schedule, **self._lease_payload(lease_id, fencing_token),
        })

    def set_schedule_enabled(self, schedule_id: str, enabled: bool, *,
                             lease_id: str | None = None, fencing_token: int | None = None) -> dict[str, Any]:
        return self._patch(f"/api/v2/schedules/{quote(schedule_id, safe='')}", {
            "enabled": enabled, **self._lease_payload(lease_id, fencing_token),
        })

    def delete_schedule(self, schedule_id: str, *, lease_id: str | None = None,
                        fencing_token: int | None = None) -> dict[str, Any]:
        return self._delete(
            f"/api/v2/schedules/{quote(schedule_id, safe='')}",
            self._lease_payload(lease_id, fencing_token),
        )

    @staticmethod
    def _lease_payload(lease_id: str | None, fencing_token: int | None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if lease_id is not None:
            payload["lease_id"] = lease_id
        if fencing_token is not None:
            payload["fencing_token"] = fencing_token
        return payload

    def get_runtime_config(self) -> dict[str, Any]:
        return self._get("/api/config")

    def update_runtime_config(self, **config: Any) -> dict[str, Any]:
        return self._post("/api/config", config)
