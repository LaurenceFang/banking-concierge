"""Small direct DeepSeek adapter. No credentials are exposed to the browser."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import DEEPSEEK_BASE, MODEL, deepseek_key


class ModelError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class DeepSeekClient:
    def __init__(self, api_key: str | None = None) -> None:
        self._key = api_key or deepseek_key()
        self.http = httpx.AsyncClient(
            base_url=DEEPSEEK_BASE,
            timeout=httpx.Timeout(50, connect=10),
            follow_redirects=False,
        )

    @property
    def configured(self) -> bool:
        return bool(self._key)

    async def close(self) -> None:
        await self.http.aclose()

    async def events(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AsyncIterator[dict]:
        if not self._key:
            raise ModelError("MODEL_NOT_CONFIGURED", "尚未配置 DeepSeek。请在服务端设置 API 密钥。")
        payload = {
            "model": MODEL,
            "messages": messages,
            "tools": tools,
            "stream": True,
            "stream_options": {"include_usage": True},
            "thinking": {"type": "disabled"},
            "max_tokens": 1600,
        }
        try:
            async with self.http.stream(
                "POST", "/chat/completions", json=payload,
                headers={"Authorization": f"Bearer {self._key}"},
            ) as response:
                if response.status_code >= 400:
                    code = "PROVIDER_RATE_LIMITED" if response.status_code == 429 else "PROVIDER_ERROR"
                    message = (
                        "模型服务当前繁忙，请稍后再试。"
                        if response.status_code == 429 else f"模型服务暂时不可用（HTTP {response.status_code}）。"
                    )
                    raise ModelError(code, message)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    if data:
                        yield json.loads(data)
        except httpx.TimeoutException as error:
            raise ModelError("PROVIDER_TIMEOUT", "模型响应超时，请重试；尚未替你执行任何资金操作。") from error
        except httpx.HTTPError as error:
            raise ModelError("PROVIDER_UNAVAILABLE", "暂时连不上模型服务，请稍后再试。") from error

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], emit=None) -> dict:
        content, reasoning, calls = "", "", {}
        async for event in self.events(messages, tools):
            choices = event.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            if delta.get("content"):
                content += delta["content"]
                if emit:
                    await emit("token", {"text": delta["content"]})
            reasoning += delta.get("reasoning_content") or ""
            for fragment in delta.get("tool_calls") or []:
                index = fragment["index"]
                call = calls.setdefault(index, {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""}
                })
                call["id"] += fragment.get("id") or ""
                function = fragment.get("function") or {}
                call["function"]["name"] += function.get("name") or ""
                call["function"]["arguments"] += function.get("arguments") or ""
        message: dict[str, Any] = {"role": "assistant", "content": content or None}
        if calls:
            message["tool_calls"] = [calls[key] for key in sorted(calls)]
        if reasoning:
            message["reasoning_content"] = reasoning
        return message
