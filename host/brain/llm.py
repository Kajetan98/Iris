"""Interfejs silnika LLM (docs/spec.md, sekcja 12). Silniki są wymienne i opcjonalne."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

Role = Literal["user", "assistant"]
FinishReason = Literal["stop", "length", "refusal", "tool_use", "other"]


@dataclass(frozen=True)
class ToolSpec:
    """Narzędzie dla modelu (docs/spec.md, sekcja 12): nazwa, opis i schemat JSON argumentów."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    """Wiadomość rozmowy. W rundzie narzędzi asystent ma `tool_calls` (i `raw`: odpowiedź silnika do
    odesłania bez zmian, np. z blokami myślenia Claude), a następna wiadomość użytkownika `tool_results`."""

    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    raw: Any = None


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
    tool_calls: tuple[ToolCall, ...] = ()
    raw: Any = None  # treść odpowiedzi w formacie silnika, do odesłania w tej samej turze


class LLMError(Exception):
    """Błąd silnika. Wywołujący nie dostaje odpowiedzi, rdzeń działa dalej."""


class LLMUnavailable(LLMError):
    """Brak połączenia, limit czasu, 429 lub 5xx. Można ponowić."""


class LLMConfigError(LLMError):
    """Zła konfiguracja: nieznany model lub endpoint. Ponowienie nie pomoże."""


class LLMRefusal(LLMError):
    """Silnik odmówił odpowiedzi."""


class LLMToolsUnsupported(LLMError):
    """Model nie obsługuje narzędzi. Zapytanie można powtórzyć bez nich."""


class LLMEngine(Protocol):
    @property
    def remote(self) -> bool:
        """Prawda, gdy dane opuszczają maszynę hosta (decyduje o polityce pamięci)."""
        ...

    async def complete(
        self, system: str, messages: Sequence[Message], *, max_tokens: int
    ) -> LLMReply:
        """Odpowiedź silnika. Silnik z narzędziami ma też `supports_tools = True` i przyjmuje
        argument `tools: Sequence[ToolSpec]`; bez tego atrybutu host narzędzi nie wysyła."""
        ...

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
