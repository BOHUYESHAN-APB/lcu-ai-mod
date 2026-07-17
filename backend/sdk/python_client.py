"""Apache-2.0 licensed integration client for the LCU companion backend."""

# Copyright 2026 LCU Mod Contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

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

    def close(self) -> None:
        self._client.close()

    def get_status(self) -> dict[str, Any]:
        return self._get("/api/status")

    def get_sdk_info(self) -> dict[str, Any]:
        return self._get("/api/sdk/info")

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

    def get_runtime_config(self) -> dict[str, Any]:
        return self._get("/api/config")

    def update_runtime_config(self, **config: Any) -> dict[str, Any]:
        return self._post("/api/config", config)
