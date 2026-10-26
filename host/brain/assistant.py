"""Fasada rozmowy: historia, kontekst, polityka faktów i wywołanie silnika."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from host.brain.llm import LLMEngine, LLMError, LLMRefusal, Message, Usage
from host.brain.prompt import SYSTEM_PROMPT, clean_for_speech, render_user_turn, select_facts
from host.core.config import LLMConfig
from host.store.models import Fact

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Answer:
    text: str
    truncated: bool  # silnik uciął odpowiedź na max_tokens
    model: str
    usage: Usage


class Assistant:
    def __init__(self, engine: LLMEngine, cfg: LLMConfig) -> None:
        self.engine = engine
        self.cfg = cfg
        self._history: list[Message] = []
        self._lock = asyncio.Lock()

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
    ) -> Answer:
        """Odpowiedź na wypowiedź. `state` to wynik HostCore.prompt_slice().

        Rzuca LLMError (w tym LLMUnavailable, LLMRefusal); historia zmienia się tylko po sukcesie.
        """
        text = text.strip()
        if not text:
            raise ValueError("pusta wypowiedź")
        chosen = select_facts(facts, self.cfg, remote=self.engine.remote)
        turn = Message("user", render_user_turn(text, state, chosen, now))
        async with self._lock:
            reply = await self.engine.complete(
                SYSTEM_PROMPT, [*self._history, turn], max_tokens=self.cfg.max_tokens
            )
            if reply.finish == "refusal":
                raise LLMRefusal("silnik odmówił odpowiedzi")
            answer = clean_for_speech(reply.text)
            if not answer:
                raise LLMError("silnik zwrócił pustą odpowiedź")
            if reply.finish == "length":
                log.warning("odpowiedź ucięta na max_tokens=%d", self.cfg.max_tokens)
            self._remember(text, answer)
            return Answer(answer, reply.finish == "length", reply.model, reply.usage)

    def _remember(self, user_text: str, answer: str) -> None:
        # W historii tylko czysta wypowiedź: kontekst dotyczy jednej tury i szybko się starzeje.
        self._history += [Message("user", user_text), Message("assistant", answer)]
        keep = self.cfg.history_turns * 2
        self._history = self._history[-keep:] if keep else []
