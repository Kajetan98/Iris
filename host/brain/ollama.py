"""Silnik Ollama przez HTTP (`POST /api/chat`, bez strumieniowania)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import urlparse

import httpx

from host.brain.llm import (
    FinishReason,
    LLMConfigError,
    LLMError,
    LLMReply,
    LLMToolsUnsupported,
    LLMUnavailable,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
    with_retries,
)

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MAX_ERROR_TEXT = 200


class OllamaEngine:
    supports_tools = True

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "http://localhost:11434",
        timeout_s: float = 30.0,
        retries: int = 1,
        temperature: float | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.model = model
        self._url = base_url.rstrip("/") + "/api/chat"
        self._host = urlparse(base_url).hostname or ""
        self._timeout = timeout_s
        self._retries = retries
        self._temperature = temperature
        self._sleep = sleep
        self._client = client
        self._owns_client = client is None

    @property
    def remote(self) -> bool:
        return self._host not in LOOPBACK_HOSTS

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def complete(
        self, system: str, messages: Sequence[Message], *, max_tokens: int, tools: Sequence[ToolSpec] = ()
    ) -> LLMReply:
        options: dict[str, Any] = {"num_predict": max_tokens}
        if self._temperature is not None:
            options["temperature"] = self._temperature
        chat: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            chat.extend(_messages(m))
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": chat,
            "stream": False,
            "options": options,
        }
        if tools:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        return await with_retries(
            lambda: self._post(payload), self._retries, sleep=self._sleep
        )

    async def _post(self, payload: dict[str, Any]) -> LLMReply:
        if self._client is None:
            self._client = httpx.AsyncClient()
        try:
            response = await self._client.post(self._url, json=payload, timeout=self._timeout)
        except httpx.TimeoutException:
            raise LLMUnavailable(f"limit czasu {self._timeout:g} s") from None
        except httpx.TransportError as e:
            raise LLMUnavailable(f"brak połączenia z {self._host}: {type(e).__name__}") from None
        # Treści zapytania ani odpowiedzi nie trafiają do logów (dane osobowe).
        log.debug("ollama %s -> %d", self._url, response.status_code)

        status = response.status_code
        if status == 404:
            raise LLMConfigError(f"Ollama zwróciła 404 (model {self.model!r} lub endpoint): {_error_text(response)}")
        if status == 429 or status >= 500:
            raise LLMUnavailable(f"Ollama zwróciła {status}: {_error_text(response)}")
        if status == 400 and "tools" in payload and "support tools" in _error_text(response).lower():
            raise LLMToolsUnsupported(f"model {self.model!r} nie obsługuje narzędzi")
        if status != 200:
            raise LLMError(f"Ollama zwróciła {status}: {_error_text(response)}")
        return self._parse(response)

    def _parse(self, response: httpx.Response) -> LLMReply:
        try:
            data = response.json()
            message = data["message"]
            text = message["content"]
            done = data.get("done", True)
        except (ValueError, KeyError, TypeError):
            raise LLMError("niepoprawna odpowiedź Ollamy") from None
        if not isinstance(text, str):
            raise LLMError("niepoprawna odpowiedź Ollamy")
        if done is not True:
            raise LLMError("odpowiedź Ollamy jest niekompletna")
        reason = data.get("done_reason")
        finish: FinishReason = (
            "stop" if reason is None else {"stop": "stop", "length": "length"}.get(reason, "other")
        )
        calls = _tool_calls(message.get("tool_calls"))
        if calls:
            finish = "tool_use"
        return LLMReply(
            text=text,
            finish=finish,
            model=str(data.get("model") or self.model),
            usage=Usage(_count(data.get("prompt_eval_count")), _count(data.get("eval_count"))),
            tool_calls=calls,
        )


def _messages(m: Message) -> list[dict[str, Any]]:
    """Wiadomość w formacie /api/chat: wywołania narzędzi przy asystencie, wyniki jako role `tool`."""
    if m.tool_results:
        return [{"role": "tool", "content": r.content, "tool_name": r.name} for r in m.tool_results]
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls]
    return [out]


def _tool_calls(raw: Any) -> tuple[ToolCall, ...]:
    if not isinstance(raw, list):
        return ()
    calls = []
    for i, item in enumerate(raw):
        fn = item.get("function") if isinstance(item, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            continue
        args = fn.get("arguments")
        if isinstance(args, str):  # niektóre wersje zwracają argumenty jako tekst JSON
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        calls.append(ToolCall(str(item.get("id") or f"call_{i}"), fn["name"], args if isinstance(args, dict) else {}))
    return tuple(calls)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
        text = body["error"] if isinstance(body, dict) else ""
    except (ValueError, KeyError):
        text = ""
    return str(text)[:MAX_ERROR_TEXT] or "brak opisu"
