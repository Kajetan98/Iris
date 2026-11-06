"""Fasada rozmowy: historia, kontekst, polityka pamięci, narzędzia i wywołanie silnika."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from host.brain.llm import (
    LLMEngine,
    LLMError,
    LLMRefusal,
    LLMReply,
    LLMToolsUnsupported,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from host.brain.prompt import SYSTEM_PROMPT, clean_for_speech, render_user_turn, select_facts
from host.brain.tools import RECALL, ToolBox
from host.core.config import LLMConfig
from host.store.models import Fact

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Answer:
    text: str
    truncated: bool  # silnik uciął odpowiedź na max_tokens
    model: str
    usage: Usage
    tool_rounds: int = 0  # ile dodatkowych zapytań do silnika zajęły narzędzia


class Assistant:
    def __init__(self, engine: LLMEngine, cfg: LLMConfig) -> None:
        self.engine = engine
        self.cfg = cfg
        self._history: list[Message] = []
        self._lock = asyncio.Lock()
        # Model, który odrzucił narzędzia, do końca sesji dostaje pamięć w bloku kontekstu (spec, sekcja 12).
        self._tools_ok = bool(getattr(engine, "supports_tools", False))

    @property
    def history(self) -> list[Message]:
        return list(self._history)

    def reset(self) -> None:
        self._history.clear()

    async def aclose(self) -> None:
        await self.engine.aclose()

    async def ask(
        self,
        text: str,
        *,
        state: dict[str, Any],
        facts: Sequence[Fact] = (),
        now: datetime | None = None,
        tools: ToolBox | None = None,
    ) -> Answer:
        """Odpowiedź na wypowiedź. `state` to wynik HostCore.prompt_slice(), `tools` to narzędzia pamięci
        tej tury (bez nich model nie dostaje pamięci).

        Rzuca LLMError (w tym LLMUnavailable, LLMRefusal); historia zmienia się tylko po sukcesie.
        Do historii trafia tylko wypowiedź i końcowa odpowiedź, bez wywołań i wyników narzędzi.
        """
        text = text.strip()
        if not text:
            raise ValueError("pusta wypowiedź")
        remote = self.engine.remote
        chosen = select_facts(facts, self.cfg, remote=remote)
        async with self._lock:
            specs = self._specs(tools, remote)
            memory = await self._context(tools, text, specs, remote)
            messages = [*self._history, Message("user", render_user_turn(text, state, chosen, now, memory))]
            rounds = 0
            while True:
                offer = specs if rounds < self.cfg.max_tool_rounds else []
                try:
                    # Po ostatniej rundzie definicje zostają (API wymaga ich przy blokach narzędzi
                    # w historii), ale silnik dostaje zakaz wywołań.
                    reply = await self._complete(messages, offer or (specs if rounds else []), final=not offer)
                except LLMToolsUnsupported:
                    log.info("model nie obsługuje narzędzi, pamięć trafi do bloku kontekstu")
                    self._tools_ok = False
                    specs = []
                    memory = await self._context(tools, text, specs, remote)
                    messages = [*self._history, Message("user", render_user_turn(text, state, chosen, now, memory))]
                    continue
                if reply.finish == "tool_use" and reply.tool_calls and offer and tools is not None:
                    results = [await _run(tools, c, offer) for c in reply.tool_calls]
                    messages += [
                        Message("assistant", reply.text, tool_calls=reply.tool_calls, raw=reply.raw),
                        Message("user", "", tool_results=tuple(results)),
                    ]
                    rounds += 1
                    continue
                break
            if reply.finish == "refusal":
                raise LLMRefusal("silnik odmówił odpowiedzi")
            answer = clean_for_speech(reply.text)
            if not answer:
                raise LLMError("silnik zwrócił pustą odpowiedź")
            if reply.finish == "length":
                log.warning("odpowiedź ucięta na max_tokens=%d", self.cfg.max_tokens)
            self._remember(text, answer)
            return Answer(answer, reply.finish == "length", reply.model, reply.usage, rounds)

    def _specs(self, tools: ToolBox | None, remote: bool) -> list[ToolSpec]:
        if tools is None or not self.cfg.tools or not self._tools_ok or self.cfg.max_tool_rounds == 0:
            return []
        return list(tools.specs(remote=remote))

    async def _context(
        self, tools: ToolBox | None, text: str, specs: Sequence[ToolSpec], remote: bool
    ) -> Sequence[str]:
        """Pojęcia w bloku kontekstu, gdy model nie dostaje `iris_recall`."""
        if tools is None or any(s.name == RECALL.name for s in specs):
            return ()
        return await tools.context(text, remote=remote)

    async def _complete(self, messages: list[Message], tools: Sequence[ToolSpec], *, final: bool = False) -> LLMReply:
        if tools:
            return await self.engine.complete(  # type: ignore[call-arg]
                SYSTEM_PROMPT, messages, max_tokens=self.cfg.max_tokens, tools=tools, allow_calls=not final
            )
        return await self.engine.complete(SYSTEM_PROMPT, messages, max_tokens=self.cfg.max_tokens)

    def _remember(self, user_text: str, answer: str) -> None:
        # W historii tylko czysta wypowiedź: kontekst dotyczy jednej tury i szybko się starzeje.
        self._history += [Message("user", user_text), Message("assistant", answer)]
        keep = self.cfg.history_turns * 2
        self._history = self._history[-keep:] if keep else []


async def _run(tools: ToolBox, call: ToolCall, offered: Sequence[ToolSpec]) -> ToolResult:
    """Wywołanie narzędzia; nieoferowane narzędzie i wyjątek dają wynik z błędem, tura trwa dalej."""
    if call.name not in {s.name for s in offered}:
        return ToolResult(call.id, call.name, f"Nieznane narzędzie: {call.name}.", is_error=True)
    try:
        return await tools.call(call)
    except Exception:  # noqa: BLE001 - narzędzie nie może przerwać rozmowy
        log.exception("błąd narzędzia %s", call.name)
        return ToolResult(call.id, call.name, "Błąd narzędzia.", is_error=True)
