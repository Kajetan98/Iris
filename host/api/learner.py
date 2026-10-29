"""Uczenie się w turach rozmowy (docs/spec.md, sekcja 9, "Uczenie się").

Łączy reguły z host/core/learning.py z bazą, zdarzeniami fact.* i silnikiem LLM. Treść faktów
trafia tylko do tabeli `facts`; zdarzenia i logi zawierają identyfikatory, nie tekst.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from host.core.clock import Clock
from host.core.config import Config
from host.core.events import HostEvent
from host.core.learning import (
    best_match,
    is_sensitive,
    parse_answer,
    parse_feedback,
    parse_memory,
    pick_question,
    question_for,
    recall_text,
)
from host.store import Store
from host.store.models import Fact

if TYPE_CHECKING:
    from host.brain.learn import Guess
    from host.brain.llm import LLMEngine

log = logging.getLogger(__name__)

DAY_S = 86400.0


@dataclass
class Pending:
    """Pytanie IRIS czekające na "tak" albo "nie"."""

    kind: Literal["fact", "forget_all"]
    until: float  # zegar monotoniczny
    fact_id: int | None = None


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    return few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many


class Learner:
    def __init__(
        self,
        config: Config,
        clock: Clock,
        store: Store,
        persist: Callable[[list[HostEvent]], None],
        guard: Callable[..., Any],
        refresh: Callable[[], None],
    ) -> None:
        self.config = config
        self.cfg = config.learning
        self.clock = clock
        self.store = store
        self._persist = persist
        self._guard = guard  # błąd bazy jest logowany, host pracuje dalej
        self._refresh = refresh
        self.pending: Pending | None = None
        self._last_question: float | None = None
        self._last_decay: float | None = None
        self._context_ids: list[int] = []  # przypuszczenia z kontekstu ostatniej odpowiedzi LLM

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    # --- zdarzenia ---

    def _event(self, type_: str, **data: Any) -> None:
        self._persist([HostEvent(type=type_, at=self.clock.wall(), data=data)])

    def _facts(self) -> list[Fact]:
        return self._guard(self.store.facts) or []

    # --- przed LLM: odpowiedź na pytanie i polecenia pamięci ---

    def answer(self, text: str) -> str | None:
        """Odpowiedź na otwarte pytanie IRIS albo None. Każda wypowiedź zamyka pytanie."""
        pending, self.pending = self.pending, None
        if not self.enabled or pending is None or self.clock.monotonic() > pending.until:
            return None
        yes, correction = parse_answer(text)
        if yes is None:
            return None
        if pending.kind == "forget_all":
            if not yes:
                return "Dobrze, niczego nie usuwam."
            count = self._guard(self.store.delete_all_facts) or 0
            self._event("fact.forget", id=None, why="all", count=count)
            self._refresh()
            return "Dobrze, usuwam wszystko, co o tobie wiem."
        fact = self._guard(self.store.get_fact, pending.fact_id)
        if fact is None:
            return "Dobrze."
        if yes:
            self._guard(self.store.update_fact, fact.id, source="stated", confidence=1.0)
            self._event("fact.confirm", id=fact.id)
            return "Dobrze, zapamiętane."
        self._guard(self.store.delete_fact, fact.id)
        self._event("fact.forget", id=fact.id, why="denied")
        if correction:
            new = self._remember(correction, how="corrected")
            self._refresh()
            return f'Dobrze, poprawione: "{new.text if new else correction}".'
        self._refresh()
        return "Dobrze, usuwam to przypuszczenie."

    def command(self, text: str) -> str | None:
        """Polecenie "zapamiętaj", "zapomnij", "zapomnij wszystko" lub "co o mnie wiesz"."""
        if not self.enabled:
            return None
        cmd = parse_memory(text)
        if cmd is None:
            return self.feedback(text)
        log.info("polecenie pamięci: %s", cmd.kind)
        if cmd.kind == "remember":
            fact = self._remember(cmd.text, how="told")
            self._refresh()
            return f'Zapamiętane: "{fact.text if fact else cmd.text}".'
        if cmd.kind == "forget":
            match = best_match(cmd.text, self._facts(), self.cfg.forget_match)
            if match is None:
                return "Nie znam takiej informacji."
            self._guard(self.store.delete_fact, match.id)
            self._event("fact.forget", id=match.id, why="user")
            self._refresh()
            return f'Zapomniane: "{match.text}".'
        if cmd.kind == "forget_all":
            count = self._guard(self.store.count_facts) or 0
            if count == 0:
                return "Nic o tobie nie wiem, nie ma czego usuwać."
            self.pending = Pending("forget_all", self.clock.monotonic() + self.cfg.answer_window_s)
            word = _plural(count, "informacja", "informacje", "informacji")
            return f"Na pewno usunąć wszystko, co o tobie wiem ({count} {word})? Powiedz tak albo nie."
        return recall_text(self._facts(), self.config.brain.llm.min_inferred_confidence)

    # --- reakcje użytkownika: nagroda dla użytych przypuszczeń ---

    def note_context(self, ids: list[int]) -> None:
        """Przypuszczenia (`inferred`), które trafiły do kontekstu ostatniej odpowiedzi LLM."""
        self._context_ids = list(ids)

    def feedback(self, text: str) -> str | None:
        """Pochwała albo krytyka odpowiedzi; None, gdy to nie reakcja albo nie było czego oceniać."""
        if not (self.enabled and self.cfg.feedback):
            return None
        kind = parse_feedback(text)
        if kind is None:
            return None
        known = {f.id: f for f in self._facts()}
        used = [known[i] for i in self._context_ids if i in known and known[i].source == "inferred"]
        if not used:
            return None
        self._context_ids = []
        log.info("reakcja użytkownika: %s (%d przypuszczeń)", kind, len(used))
        if kind == "praise":
            for f in used:
                fact = self._guard(self.store.reinforce_fact, f.id, self.cfg.feedback_up, self.cfg.max_inferred)
                if fact is not None:
                    self._event("fact.learn", id=fact.id, source="inferred", confidence=fact.confidence, how="praised")
            self._refresh()
            return "Dobrze, zapisuję, że to było trafne."
        for f in used:
            conf = round(max(0.0, f.confidence - self.cfg.feedback_down), 6)
            if conf < self.cfg.forget_below:
                self._guard(self.store.delete_fact, f.id)
                self._event("fact.forget", id=f.id, why="criticized")
            else:
                fact = self._guard(self.store.update_fact, f.id, confidence=conf)
                if fact is not None:
                    self._event("fact.learn", id=fact.id, source="inferred", confidence=fact.confidence, how="criticized")
        self._refresh()
        return "Dobrze, osłabiam przypuszczenia użyte w tej odpowiedzi."

    def _remember(self, text: str, *, how: str) -> Fact | None:
        """Fakt podany wprost; podobny istniejący fakt zostaje zastąpiony nowym tekstem."""
        match = best_match(text, self._facts(), self.cfg.similar)
        if match is not None:
            fact = self._guard(self.store.update_fact, match.id, text=text, source="stated", confidence=1.0)
        else:
            fact = self._guard(self.store.add_fact, text, "stated")
        if fact is not None:
            self._event("fact.learn", id=fact.id, source="stated", confidence=1.0, how=how)
        return fact

    def forget(self, fact_id: int) -> bool:
        """Usunięcie przez użytkownika (DELETE /facts)."""
        if not self.store.delete_fact(fact_id):
            return False
        self._event("fact.forget", id=fact_id, why="user")
        self._refresh()
        return True

    # --- po odpowiedzi LLM: pytanie i wnioski ---

    def question(self) -> str | None:
        """Pytanie o przypuszczenie, dopisywane do odpowiedzi, albo None."""
        if not (self.enabled and self.cfg.ask) or self.config.initiative.quiet or self.pending is not None:
            return None
        now = self.clock.monotonic()
        if self._last_question is not None and now - self._last_question < self.cfg.ask_interval_s:
            return None
        fact = pick_question(self._facts(), self.clock.wall(), self.cfg)
        if fact is None:
            return None
        self._guard(self.store.mark_fact_asked, fact.id)
        self._event("fact.ask", id=fact.id)
        self._last_question = now
        self.pending = Pending("fact", now + self.cfg.answer_window_s, fact.id)
        return question_for(fact)

    def infers_with(self, engine: "LLMEngine") -> bool:
        policy = self.cfg.infer
        return self.enabled and self.cfg.max_per_turn > 0 and (
            policy == "always" or (policy == "local_only" and not engine.remote)
        )

    async def learn_from(self, engine: "LLMEngine", utterance: str) -> None:
        """Wnioski z wypowiedzi w tle. Błąd silnika jest logowany bez treści i pomijany."""
        from host.brain.learn import LearnError, infer_facts
        from host.brain.llm import LLMError

        try:
            guesses = await infer_facts(engine, utterance, limit=self.cfg.max_per_turn)
        except (LLMError, LearnError) as e:
            log.warning("wnioski z rozmowy pominięte (%s)", type(e).__name__)
            return
        self.merge(guesses)

    def merge(self, guesses: list["Guess"]) -> None:
        dropped = 0
        for g in guesses:
            if g.confidence < self.cfg.forget_below or is_sensitive(g.text, self.cfg.sensitive_words):
                dropped += 1
                continue
            match = best_match(g.text, self._facts(), self.cfg.similar)
            if match is not None and match.source == "stated":
                continue
            if match is not None:
                fact = self._guard(self.store.reinforce_fact, match.id, self.cfg.reinforce_step, self.cfg.max_inferred)
                how = "reinforced"
            else:
                fact = self._guard(self.store.add_fact, g.text, "inferred", min(g.confidence, self.cfg.start_max))
                how = "inferred"
            if fact is not None:
                self._event("fact.learn", id=fact.id, source="inferred", confidence=fact.confidence, how=how)
        if dropped:
            log.info("wnioski z rozmowy: pominięte %d (wrażliwe lub niepewne)", dropped)
        self._refresh()

    # --- zanikanie ---

    def maybe_decay(self) -> None:
        """Raz na dobę (i przy pierwszym wywołaniu): słabną przypuszczenia bez zmian od decay_after_days."""
        now = self.clock.monotonic()
        if not self.enabled or (self._last_decay is not None and now - self._last_decay < DAY_S):
            return
        self._last_decay = now
        from datetime import timedelta

        older = self.clock.wall() - timedelta(days=self.cfg.decay_after_days)
        result = self._guard(self.store.decay_facts, older, self.cfg.decay_step, self.cfg.forget_below)
        if not result:
            return
        weakened, forgotten = result
        for fact_id in forgotten:
            self._event("fact.forget", id=fact_id, why="decay")
        if weakened or forgotten:
            log.info("zanikanie: osłabione %d, zapomniane %d", len(weakened), len(forgotten))
            self._refresh()
