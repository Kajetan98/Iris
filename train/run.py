"""Uruchamianie scenariuszy prawdziwą ścieżką rozmowy hosta (docs/spec.md, sekcja 15).

Scenariusz przechodzi przez te same części co POST /chat: polecenia pamięci obsługuje host, reszta trafia
do Assistant z narzędziami pamięci i blokiem kontekstu. RecordingEngine zapisuje każde zapytanie do silnika
dokładnie w tej postaci, w jakiej model dostanie je po wdrożeniu, więc zbiór treningowy nie może rozjechać
się z hostem.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from host.api.service import HostService
from host.brain import Assistant
from host.brain.learn import CONCEPTS_PROMPT
from host.brain.llm import LLMEngine, LLMError, LLMReply, Message, ToolCall, ToolSpec, Usage
from host.core.clock import ManualClock
from host.core.config import Config
from host.store import Store
from train import checks
from train.scenarios import Scenario, Turn

# Dane są syntetyczne, więc pamięć i wyciąganie pojęć idą do każdego silnika, także zdalnego.
CONFIG = {"brain": {"llm": {"engine": "ollama", "model": "train", "send_memory": "always"}},
          "memory": {"infer": "always"}}


@dataclass(frozen=True)
class Call:
    """Jedno zapytanie do silnika i jego odpowiedź."""

    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]  # narzędzia, które model mógł wywołać w tym zapytaniu
    reply: LLMReply

    @property
    def task(self) -> str:
        return "concepts" if self.system == CONCEPTS_PROMPT else "chat"


@dataclass
class TurnResult:
    turn: Turn
    host_reply: str | None = None  # wypowiedź obsłużył host bez modelu (polecenie pamięci)
    answer: str | None = None
    calls: list[Call] = field(default_factory=list)
    error: str | None = None

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [c for call in self.calls if call.task == "chat" for c in call.reply.tool_calls]


class RecordingEngine:
    """Silnik pośredni: przekazuje zapytania dalej i je zapisuje. `supports_tools` wybiera tryb rozmowy:
    narzędzia pamięci albo pamięć w bloku kontekstu (spec, sekcja 12)."""

    def __init__(self, inner: LLMEngine, *, tools: bool) -> None:
        self.inner = inner
        self.supports_tools = tools
        self.calls: list[Call] = []

    @property
    def remote(self) -> bool:
        return self.inner.remote

    async def complete(self, system: str, messages: Sequence[Message], *, max_tokens: int,
                       tools: Sequence[ToolSpec] = (), allow_calls: bool = True) -> LLMReply:
        if tools:
            reply = await self.inner.complete(  # type: ignore[call-arg]
                system, messages, max_tokens=max_tokens, tools=tools, allow_calls=allow_calls)
        else:
            reply = await self.inner.complete(system, messages, max_tokens=max_tokens)
        self.calls.append(Call(system, tuple(messages), tuple(tools) if allow_calls else (), reply))
        return reply

    async def aclose(self) -> None:
        await self.inner.aclose()


class GoldEngine:
    """Odpowiada wzorcowymi odpowiedziami bieżącej tury (train/scenarios.py, GOLD)."""

    remote = False
    supports_tools = True

    def __init__(self) -> None:
        self._replies: list[LLMReply] = []
        self._concepts = ""

    def load(self, turn: Turn, *, tools: bool) -> None:
        if turn.answer is None:
            raise ValueError(f"tura bez odpowiedzi wzorcowej: {turn.text}")
        self._replies = []
        if tools and turn.call is not None:
            call = ToolCall("gold-1", turn.call["name"], dict(turn.call["arguments"]))
            self._replies.append(LLMReply("", "tool_use", "gold", Usage(), (call,)))
            self._replies.append(LLMReply(turn.answer, "stop", "gold"))
        else:
            self._replies.append(LLMReply(turn.answer if tools else (turn.answer_no_tools or turn.answer), "stop", "gold"))
        self._concepts = json.dumps(turn.concepts, ensure_ascii=False) if turn.concepts is not None else ""

    async def complete(self, system: str, messages: Sequence[Message], *, max_tokens: int,
                       tools: Sequence[ToolSpec] = (), allow_calls: bool = True) -> LLMReply:
        if system == CONCEPTS_PROMPT:
            return LLMReply(self._concepts, "stop", "gold")
        if not self._replies:
            raise LLMError("scenariusz wzorcowy nie przewiduje kolejnego zapytania")
        return self._replies.pop(0)

    async def aclose(self) -> None:
        return None


async def run_scenario(scenario: Scenario, engine: LLMEngine, *, tools: bool) -> list[TurnResult]:
    """Rozmowa ze scenariusza na świeżym hoście z pustą bazą w pamięci procesu."""
    cfg = Config.model_validate(CONFIG)
    clock = ManualClock(wall=scenario.now)
    store = Store(clock=clock, memory=cfg.memory)
    recorder = RecordingEngine(engine, tools=tools)
    assistant = Assistant(recorder, cfg.brain.llm)
    service = HostService(cfg, clock, store, assistant=assistant)
    service.start()
    results: list[TurnResult] = []
    try:
        for sentence in scenario.teach:
            service.memory.teach(sentence)
        state: dict[str, Any] = dict(scenario.state)
        for turn in scenario.turns:
            result = TurnResult(turn)
            results.append(result)
            # Kolejność jak w HostService.chat: najpierw polecenia pamięci, bez modelu.
            result.host_reply = service.memory.answer(turn.text) or service.memory.command(turn.text)
            if result.host_reply is not None:
                continue
            if isinstance(engine, GoldEngine):
                engine.load(turn, tools=tools)
            before = len(recorder.calls)
            turn_tools = service.memory.tools()
            try:
                answer = await assistant.ask(turn.text, state=state, now=clock.wall(), tools=turn_tools)
                result.answer = answer.text
            except LLMError as e:
                result.error = f"{type(e).__name__}: {e}"
            service.memory.note_recalled(turn_tools.recalled)
            if not isinstance(engine, GoldEngine) or turn.concepts is not None:
                service.memory.submit(recorder, turn.text)  # nauka w tle: zapytanie o pojęcia z wypowiedzi
                await service.memory.drain()
            result.calls = recorder.calls[before:]
            # Zmiany stanu i przerwa są raportowane raz, jak w prompt_slice (spec, sekcja 7).
            state = {k: v for k, v in state.items() if k not in ("body_changes", "last_downtime_s", "device_change")}
    finally:
        await service.drain_voice()  # kończy też zadanie nauki w tle
        service.stop()
        store.close()
    return results


def judge(result: TurnResult, *, tools: bool) -> list[str]:
    """Naruszone reguły w turze: kontrole odpowiedzi i narzędzi oraz oczekiwania scenariusza."""
    turn = result.turn
    if result.host_reply is not None:
        return ["handled_by_host"]  # błąd scenariusza: taka tura nie uczy modelu niczego
    if result.error is not None or result.answer is None:
        return ["error"]
    failed = list(checks.check_reply(_final_text(result)))
    for call in result.calls:
        if call.task == "chat" and call.reply.tool_calls:
            failed += checks.check_tool_calls(call.reply.tool_calls, call.tools, turn.text)
    used = {c.name for c in result.tool_calls}
    if tools and turn.expect_tool is not None and turn.expect_tool not in used:
        failed.append("tool_missing")
    if "iris_remember" in used and turn.expect_tool != "iris_remember":
        failed.append("remember_unasked")
    if turn.mention and not checks.mentions(result.answer, turn.mention):
        failed.append("mention_missing")
    return list(dict.fromkeys(failed))


def _final_text(result: TurnResult) -> str:
    """Odpowiedź końcowa w postaci z silnika, przed oczyszczeniem do mowy (kontrola markdownu)."""
    chat = [c for c in result.calls if c.task == "chat"]
    return chat[-1].reply.text if chat else (result.answer or "")
