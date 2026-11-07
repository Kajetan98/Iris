"""Pamięć IRIS w hoście (docs/spec.md, sekcja 9, "Pamięć: graf pojęć").

Łączy reguły z host/core/memory.py z bazą, zdarzeniami memory.* i silnikiem LLM. Pojęcia są tylko
w tabelach grafu; zdarzenia i logi zawierają liczby, nie treść.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from host.core import memory as m
from host.core.clock import Clock
from host.core.config import MEMORY_DOMAINS, MEMORY_DOMAIN_IDS, Config
from host.core.events import HostEvent
from host.store import Store
from host.store.models import Concept

if TYPE_CHECKING:
    from host.brain.llm import LLMEngine, ToolCall, ToolResult, ToolSpec

log = logging.getLogger(__name__)

MAX_QUERY_CHARS = 500


class Memory:
    def __init__(
        self,
        config: Config,
        clock: Clock,
        store: Store,
        persist: Callable[[list[HostEvent]], None],
        guard: Callable[..., Any],
        refresh: Callable[[], None],
        spawn: Callable[[Any], None],
    ) -> None:
        self.config = config
        self.cfg = config.memory
        self.clock = clock
        self.store = store
        self._persist = persist
        self._guard = guard  # błąd bazy jest logowany, host pracuje dalej
        self._refresh = refresh
        self._spawn = spawn
        self._forget_all_until: float | None = None  # pytanie "zapomnij wszystko" czeka na tak/nie
        self._recalled: list[str] = []  # klucze przypomniane do ostatniej odpowiedzi LLM
        self._last_prune: float | None = None
        self._listeners: list[Callable[[], None]] = []
        self._queue: asyncio.Queue[tuple["LLMEngine", str]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None  # pętla, w której działa kolejka nauki

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    # --- zdarzenia i powiadomienia ---

    def _event(self, type_: str, **data: Any) -> None:
        self._persist([HostEvent(type=type_, at=self.clock.wall(), data=data)])

    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Powiadomienie o każdej zmianie pamięci (strumień /memory/stream)."""
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _changed(self) -> None:
        self._refresh()
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001 - słuchacz nie może zatrzymać pamięci
                log.exception("błąd słuchacza pamięci")

    # --- operacje na grafie ---

    def learn(self, ext: m.Extraction, *, how: str, domain: str | None = None, source: str | None = None) -> m.LearnPlan:
        keys = [k for k, _ in ext.concepts]
        known = self._guard(self.store.concepts, keys) or {}
        links = self._guard(self.store.links_between, keys) or {}
        plan = m.plan_learn(ext, known, links, self.clock.wall(), self.cfg, domain)
        if not plan.concepts:
            return plan
        self._guard(self.store.save_memory, plan.concepts, plan.links)
        data: dict[str, Any] = {"how": how, "fresh": len(plan.fresh), "reinforced": len(plan.reinforced),
                                "domain": plan.domain}
        if source is not None:
            data["source"] = source
        self._event("memory.learn", **data)
        self._changed()
        return plan

    def recall(self, query: str, *, by: str, k: int | None = None) -> list[m.RecallHit]:
        keys = [key for key, _ in m.heuristic(query, self.cfg).concepts]
        if not keys:
            return []
        matched = self._guard(self.store.concepts, keys) or {}
        links = self._guard(self.store.links_touching, list(matched)) or []
        neighbours = {l.b if l.a in matched else l.a for l in links} - set(matched)
        known = {**matched, **(self._guard(self.store.concepts, neighbours) or {})}
        hits, reinforced = m.plan_recall(keys, known, links, self.clock.wall(), self.cfg, k)
        if reinforced:
            self._guard(self.store.save_memory, reinforced)
        self._event("memory.recall", by=by, count=len(hits))
        if reinforced:
            self._changed()
        return hits

    def forget(self, keys: Sequence[str], *, why: str) -> list[Concept]:
        removed = self._guard(self.store.delete_concepts, keys) or []
        if removed:
            self._event("memory.forget", why=why, count=len(removed))
            self._changed()
        return removed

    def clear(self) -> int:
        count = self._guard(self.store.clear_memory) or 0
        self._event("memory.forget", why="all", count=count)
        self._changed()
        return count

    def maybe_prune(self) -> None:
        """Przy starcie i co `prune_every_s`: usuwa pojęcia i połączenia poniżej progu (spec, sekcja 9)."""
        now = self.clock.monotonic()
        if not self.enabled or (self._last_prune is not None and now - self._last_prune < self.cfg.prune_every_s):
            return
        self._last_prune = now
        concepts = (self._guard(self.store.concepts) or {}).values()
        dead, weak = m.plan_prune(concepts, self._guard(self.store.all_links) or [], self.clock.wall(), self.cfg)
        if not dead and not weak:
            return
        self._guard(self.store.save_memory, delete=dead, delete_links=weak, forgotten=len(dead))
        if dead:
            self._event("memory.forget", why="decay", count=len(dead))
        log.info("zanik pamięci: zapomniane pojęcia %d, połączenia %d", len(dead), len(weak))
        self._changed()

    def snapshot(self) -> dict[str, Any]:
        """Stan grafu jak w GET /memory (spec, sekcja 10). Odczyt nie wzmacnia pojęć."""
        now = self.clock.wall()
        concepts = (self._guard(self.store.concepts) or {}).values()
        links = self._guard(self.store.all_links) or []
        return {
            "now": _iso(now),
            "params": {"half_life_days": self.cfg.half_life_days, "gain_concept": self.cfg.gain_concept,
                       "gain_link": self.cfg.gain_link, "gain_recall": self.cfg.gain_recall,
                       "forget_below": self.cfg.forget_below},
            "domains": MEMORY_DOMAINS,
            "nodes": [
                {"key": c.key, "label": c.label, "strength": round(c.strength, 6),
                 "effective": round(m.concept_strength(c, now, self.cfg), 4), "last_used": _iso(c.last_used),
                 "hits": c.hits, "contexts": c.domains}
                for c in m.strongest(concepts, now, self.cfg, len(concepts))
            ],
            "links": [
                {"a": l.a, "b": l.b, "strength": round(l.strength, 6),
                 "effective": round(m.link_strength(l, now, self.cfg), 4), "last_used": _iso(l.last_used)}
                for l in sorted(links, key=lambda l: (l.a, l.b))
            ],
            "forgotten": self._guard(self.store.forgotten_count) or 0,
        }

    # --- przed LLM: odpowiedź na pytanie, polecenia, reakcje ---

    def answer(self, text: str) -> str | None:
        """Odpowiedź na pytanie z "zapomnij wszystko" albo None. Każda wypowiedź zamyka pytanie."""
        until, self._forget_all_until = self._forget_all_until, None
        if not self.enabled or until is None or self.clock.monotonic() > until:
            return None
        yes = m.parse_answer(text)
        if yes is None:
            return None
        if not yes:
            return m.FORGET_ALL_KEPT
        self.clear()
        return m.FORGET_ALL_DONE

    def command(self, text: str) -> str | None:
        """Polecenie pamięci albo reakcja na ostatnią odpowiedź; None, gdy wypowiedź trafia do LLM."""
        if not self.enabled:
            return None
        cmd = m.parse_command(text)
        if cmd is None:
            return self.feedback(text)
        log.info("polecenie pamięci: %s", cmd.kind)
        if cmd.kind == "remember":
            ext = m.keep(m.heuristic(cmd.text, self.cfg), self.cfg, allow_sensitive=True)
            plan = self.learn(ext, how="told")
            return m.remember_text([label for _, label in ext.concepts] if plan.concepts else [])
        if cmd.kind == "forget":
            keys = [k for k, _ in m.heuristic(cmd.text, self.cfg).concepts]
            return m.forget_text([c.label for c in self.forget(keys, why="user")])
        total = self._guard(self.store.concept_count) or 0
        if cmd.kind == "forget_all":
            if total == 0:
                return m.forget_all_question(0)
            self._forget_all_until = self.clock.monotonic() + self.cfg.answer_window_s
            return m.forget_all_question(total)
        top = m.strongest((self._guard(self.store.concepts) or {}).values(), self.clock.wall(), self.cfg, 8)
        return m.recall_text([c.label for c in top], total)

    def note_recalled(self, keys: Sequence[str]) -> None:
        """Pojęcia przypomniane do ostatniej odpowiedzi LLM (narzędziem albo w kontekście)."""
        self._recalled = list(dict.fromkeys(keys))

    def feedback(self, text: str) -> str | None:
        """Pochwała albo krytyka; None, gdy to nie reakcja albo do odpowiedzi nie przypomniano pojęć."""
        if not (self.enabled and self.cfg.feedback):
            return None
        kind = m.parse_feedback(text)
        if kind is None:
            return None
        used = list((self._guard(self.store.concepts, self._recalled) or {}).values())
        if not used:
            return None
        self._recalled = []
        log.info("reakcja użytkownika: %s (%d pojęć)", kind, len(used))
        now = self.clock.wall()
        if kind == "praise":
            updated = m.plan_praise(used, now, self.cfg)
            self._guard(self.store.save_memory, updated)
            self._event("memory.learn", how="praised", fresh=0, reinforced=len(updated), domain=None)
            self._changed()
            return m.PRAISE_TEXT
        kept, dead = m.plan_criticism(used, now, self.cfg)
        self._guard(self.store.save_memory, kept, delete=dead)
        if kept:
            self._event("memory.learn", how="criticized", fresh=0, reinforced=len(kept), domain=None)
        if dead:
            self._event("memory.forget", why="criticized", count=len(dead))
        self._changed()
        return m.CRITICISM_TEXT

    # --- narzędzia tury i pamięć w kontekście ---

    def allowed_for(self, remote: bool) -> bool:
        policy = self.config.brain.llm.send_memory
        return self.enabled and (policy == "always" or (policy == "local_only" and not remote))

    def tools(self) -> "TurnTools":
        return TurnTools(self)

    # --- nauka z rozmowy w tle ---

    def submit(self, engine: "LLMEngine", utterance: str) -> None:
        """Wypowiedź do nauki po turze rozmowy. Tura na to nie czeka; nadmiar kolejki jest pomijany."""
        if not self.enabled:
            return
        if m.is_sensitive(utterance, self.cfg.sensitive_words):
            log.info("nauka z rozmowy pominięta: dane wrażliwe")
            return
        loop = asyncio.get_running_loop()
        if self._queue is None or self._loop is not loop:
            # Kolejka i jej wątek roboczy należą do jednej pętli zdarzeń (nowa pętla: nowa kolejka).
            self._queue, self._loop = asyncio.Queue(maxsize=self.cfg.queue_size), loop
            self._spawn(self._worker(self._queue))
        try:
            self._queue.put_nowait((engine, utterance))
        except asyncio.QueueFull:
            log.warning("kolejka nauki pełna, wypowiedź pominięta")

    async def _worker(self, queue: asyncio.Queue[tuple["LLMEngine", str]]) -> None:
        while True:
            engine, utterance = await queue.get()
            try:
                await self.learn_from(engine, utterance)
            except Exception:  # noqa: BLE001 - nauka nie może zatrzymać kolejki
                log.exception("błąd nauki z rozmowy")
            finally:
                queue.task_done()

    async def drain(self) -> None:
        """Czeka, aż kolejka nauki się opróżni (testy, zamykanie)."""
        if self._queue is not None and self._loop is asyncio.get_running_loop():
            await self._queue.join()

    def uses_llm(self, engine: "LLMEngine") -> bool:
        policy = self.cfg.infer
        return policy == "always" or (policy == "local_only" and not engine.remote)

    async def learn_from(self, engine: "LLMEngine", utterance: str) -> None:
        ext: m.Extraction | None = None
        if self.uses_llm(engine):
            from host.brain.learn import LearnError, extract_concepts
            from host.brain.llm import LLMError

            try:
                ext = await extract_concepts(engine, utterance, self.cfg)
            except (LLMError, LearnError) as e:
                log.warning("pojęcia z LLM pominięte (%s), używam heurystyki", type(e).__name__)
        if ext is None:
            ext = m.heuristic(utterance, self.cfg)
        ext = m.keep(ext, self.cfg, allow_sensitive=False)
        self.learn(ext, how="conversation", source=ext.source)


class TurnTools:
    """Narzędzia pamięci jednej tury rozmowy (host/brain/tools.py, ToolBox)."""

    def __init__(self, memory: Memory) -> None:
        self.memory = memory
        self.recalled: list[str] = []

    def specs(self, *, remote: bool) -> list["ToolSpec"]:
        from host.brain.tools import RECALL, REMEMBER

        if not self.memory.enabled:
            return []
        return [RECALL, REMEMBER] if self.memory.allowed_for(remote) else [REMEMBER]

    async def call(self, call: "ToolCall") -> "ToolResult":
        from host.brain.llm import ToolResult

        args = call.arguments
        if call.name == "iris_recall":
            query = args.get("query")
            if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
                return ToolResult(call.id, call.name, "Argument query musi być tekstem (1-500 znaków).", is_error=True)
            hits = self.memory.recall(query, by="tool")
            self.recalled += [h.key for h in hits]
            return ToolResult(call.id, call.name, m.tool_recall_text(hits))
        if call.name == "iris_remember":
            text, domain = args.get("text"), args.get("domain")
            if not isinstance(text, str) or not text.strip() or len(text) > m.MAX_COMMAND_CHARS:
                return ToolResult(call.id, call.name, "Argument text musi być tekstem (1-300 znaków).", is_error=True)
            if domain is not None and domain not in MEMORY_DOMAIN_IDS:
                return ToolResult(call.id, call.name, "Nieznana domena.", is_error=True)
            ext = m.keep(m.heuristic(text, self.memory.cfg), self.memory.cfg, allow_sensitive=False)
            plan = self.memory.learn(ext, how="tool", domain=domain)
            return ToolResult(call.id, call.name, m.tool_remember_text(plan.fresh, plan.reinforced))
        return ToolResult(call.id, call.name, f"Nieznane narzędzie: {call.name}.", is_error=True)

    async def context(self, text: str, *, remote: bool) -> list[str]:
        if not self.memory.allowed_for(remote):
            return []
        hits = self.memory.recall(text, by="context")
        self.recalled += [h.key for h in hits]
        return m.tool_recall_text(hits).splitlines() if hits else []


def _iso(dt: datetime) -> str:
    from datetime import timezone

    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")
