"""Interfejs silnika LLM (docs/spec.md, sekcja 12). Silniki są wymienne i opcjonalne."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

Role = Literal["user", "assistant"]
FinishReason = Literal["stop", "length", "refusal", "other"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class LLMReply:
    text: str
    finish: FinishReason
    model: str
    usage: Usage = Usage()


class LLMError(Exception):
    """Błąd silnika. Wywołujący nie dostaje odpowiedzi, rdzeń działa dalej."""


class LLMUnavailable(LLMError):
    """Brak połączenia, limit czasu, 429 lub 5xx. Można ponowić."""


class LLMConfigError(LLMError):
    """Zła konfiguracja: nieznany model lub endpoint. Ponowienie nie pomoże."""


class LLMRefusal(LLMError):
    """Silnik odmówił odpowiedzi."""


class LLMEngine(Protocol):
    @property
    def remote(self) -> bool:
        """Prawda, gdy dane opuszczają maszynę hosta (decyduje o polityce faktów)."""
        ...

    async def complete(
        self, system: str, messages: Sequence[Message], *, max_tokens: int
    ) -> LLMReply: ...

    async def aclose(self) -> None: ...


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    retries: int,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    base_delay_s: float = 0.5,
) -> T:
    """Ponawia tylko LLMUnavailable, z opóźnieniem rosnącym dwukrotnie."""
    attempt = 0
    while True:
        try:
            return await fn()
        except LLMUnavailable as e:
            if attempt >= retries:
                raise
            delay = base_delay_s * 2**attempt
            attempt += 1
            log.warning("silnik LLM niedostępny (%s), próba %d za %.1f s", e, attempt + 1, delay)
            await sleep(delay)
