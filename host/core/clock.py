"""Zegar wstrzykiwany do logiki stanu. Testy używają ManualClock."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float:
        """Sekundy z zegara monotonicznego."""
        ...

    def wall(self) -> datetime:
        """Czas ścienny ze strefą (godziny aktywności liczone w jej czasie lokalnym)."""
        ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> datetime:
        return datetime.now().astimezone()


class ManualClock:
    """Zegar sterowany ręcznie; oba odczyty przesuwają się razem."""

    def __init__(self, wall: datetime | None = None, monotonic: float = 0.0) -> None:
        self._wall = wall or datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self._mono = monotonic

    def monotonic(self) -> float:
        return self._mono

    def wall(self) -> datetime:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._wall += timedelta(seconds=seconds)
