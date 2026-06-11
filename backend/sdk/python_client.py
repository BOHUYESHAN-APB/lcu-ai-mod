"""Lightweight upstream integration client for LCU Mod backend."""

from __future__ import annotations

from typing import Any

import httpx


class LCUClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8080", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def get_status(self) -> dict[str, Any]:
        return self._client.get("/api/status").json()

    def list_provider_presets(self) -> list[dict[str, Any]]:
        return self._client.get("/api/llm/providers").json().get("providers", [])

    def set_llm_config(self, agent: str = "default", **config: Any) -> dict[str, Any]:
        payload = {"agent": agent, **config}
        return self._client.post("/api/llm/config", json=payload).json()

    def fetch_models(self, agent: str = "default", **overrides: Any) -> list[str]:
        payload = {"agent": agent, **overrides}
        return self._client.post("/api/llm/models", json=payload).json().get("models", [])

    def get_persona(self) -> dict[str, Any]:
        return self._client.get("/api/persona").json()

    def set_persona(self, **persona: Any) -> dict[str, Any]:
        return self._client.post("/api/persona", json=persona).json()

    def push_external_context(self, external_context: dict[str, Any]) -> dict[str, Any]:
        return self._client.post("/api/sdk/context", json={"external_context": external_context}).json()

    def update_runtime_config(self, **config: Any) -> dict[str, Any]:
        return self._client.post("/api/config", json=config).json()
