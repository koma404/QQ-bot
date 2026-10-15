"""OpenRouter (OpenAI-compatible) chat client."""
from __future__ import annotations

import asyncio

import httpx

from config import Config


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = httpx.AsyncClient(timeout=60)

    async def chat(self, system: str, messages: list[dict]) -> str:
        if not self.cfg.openrouter_api_key:
            raise LLMError("未配置 openrouter_api_key")
        payload = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.cfg.openrouter_api_key}",
            "X-Title": "qq-llm-bot",
        }
        last: Exception | None = None
        for attempt in range(2):
            try:
                r = await self.client.post(
                    f"{self.cfg.base_url.rstrip('/')}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    await asyncio.sleep(2)
                    continue
                r.raise_for_status()
                return (r.json()["choices"][0]["message"]["content"] or "").strip()
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                last = e
                if attempt == 0:
                    await asyncio.sleep(2)
        raise LLMError(f"OpenRouter 调用失败: {last!r}")
