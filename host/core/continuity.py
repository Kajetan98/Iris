"""Ciągłość między sesjami (docs/spec.md, sekcja 9). Zapis do SQLite należy do host/store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Continuity:
    sessions: int  # łącznie z bieżącą
    known_facts: int
    last_downtime_s: int
    last_restart: datetime

    @classmethod
    def at_start(
        cls,
        now: datetime,
        prev_last_event: datetime | None,
        prior_sessions: int = 0,
        known_facts: int = 0,
    ) -> "Continuity":
        """Wyznacza przerwę z ostatniego zdarzenia poprzedniej sesji (brak = pierwsze uruchomienie)."""
        if prev_last_event is None:
            downtime = 0
        else:
            downtime = max(0, int((now - prev_last_event).total_seconds()))
        return cls(
            sessions=prior_sessions + 1,
            known_facts=known_facts,
            last_downtime_s=downtime,
            last_restart=now,
        )

    def snapshot(self) -> dict[str, int]:
        return {"sessions": self.sessions, "known_facts": self.known_facts}
