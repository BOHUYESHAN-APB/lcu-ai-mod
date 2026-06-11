"""
LLM Provider — Xiaomi MiMo API client (OpenAI-compatible).
Also supports any OpenAI-compatible API.

Usage:
    provider = MiMoProvider()
    response = provider.chat([{"role": "user", "content": "hello"}])
"""

import os
import json
from typing import Optional
import httpx

# Default MiMo API config (OpenAI-compatible)
MIMO_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
MIMO_MODELS = [
    "mimo-v2.5-pro",
    "mimo-v2.5",
    "mimo-v2-pro",
    "mimo-v2-omni",
    "mimo-v2-flash",
]


class MiMoProvider:
    """OpenAI-compatible LLM provider. Default: Xiaomi MiMo."""

    def __init__(self):
        # Priority: cookie (from web dashboard) > env var > None
        self.api_key = self._load_api_key()
        self.base_url = os.getenv("LLM_BASE_URL", MIMO_BASE_URL)
        self.model = os.getenv("LLM_MODEL", "mimo-v2.5-pro")
        self.temperature = float(os.getenv("LLM_TEMPERATURE", "1.0"))

    def _load_api_key(self) -> Optional[str]:
        """Load API key from env or cookie file."""
        key = os.environ.get("MIMO_API_KEY")
        if key:
            return key
        cookie_file = os.path.join(os.path.dirname(__file__), "..", ".api_key")
        if os.path.exists(cookie_file):
            with open(cookie_file) as f:
                return f.read().strip()
        return None

    def set_api_key(self, key: str):
        """Set API key in memory (called by web dashboard)."""
        self.api_key = key
        os.environ["MIMO_API_KEY"] = key

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def fetch_models(self) -> list[str]:
        """Fetch available models from the API."""
        if not self.api_key:
            return MIMO_MODELS  # fallback to default list
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.base_url}/models",
                    headers={"api-key": self.api_key},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return [m["id"] for m in data.get("data", [])]
        except Exception:
            pass
        return MIMO_MODELS

    def chat(
        self,
        messages: list[dict],
        max_tokens: int = 4096,
    ) -> Optional[str]:
        """Send a chat completion request. Returns the response text."""
        if not self.api_key:
            return "[LLM 未配置] 请在设置中填入 MiMo API Key"

        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "max_completion_tokens": max_tokens,
                    "temperature": self.temperature,
                },
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            return f"[LLM 错误] HTTP {e.response.status_code}: {e.response.text}"
        except Exception as e:
            return f"[LLM 错误] {str(e)}"
