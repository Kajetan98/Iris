"""Silnik Ollama przez HTTP (`POST /api/chat`, bez strumieniowania)."""

from __future__ import annotations

import asyncio
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
    LLMUnavailable,
    Message,
    Usage,
    with_retries,
)

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MAX_ERROR_TEXT = 200


class OllamaEngine:
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
        self, system: str, messages: Sequence[Message], *, max_tokens: int
    ) -> LLMReply:
        options: dict[str, Any] = {"num_predict": max_tokens}
        if self._temperature is not None:
            options["temperature"] = self._temperature
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": options,
        }
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
        return LLMReply(
            text=text,
            finish=finish,
            model=str(data.get("model") or self.model),
            usage=Usage(_count(data.get("prompt_eval_count")), _count(data.get("eval_count"))),
        )


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
        text = body["error"] if isinstance(body, dict) else ""
    except (ValueError, KeyError):
        text = ""
    return str(text)[:MAX_ERROR_TEXT] or "brak opisu"
