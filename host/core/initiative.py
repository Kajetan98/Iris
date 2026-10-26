"""Bramka inicjatywy: wypowiedź bez wywołania użytkownika (docs/spec.md, sekcja 8)."""

from __future__ import annotations

from collections import deque

from host.core.clock import Clock
from host.core.config import InitiativeConfig

WINDOW_S = 3600.0


class InitiativeGate:
    def __init__(self, config: InitiativeConfig, clock: Clock) -> None:
        self._max = config.max_per_hour
        self._clock = clock
        self._grants: deque[float] = deque()
        self.quiet = config.quiet

    def _prune(self, now: float) -> None:
        while self._grants and self._grants[0] <= now - WINDOW_S:
            self._grants.popleft()

    def allowed(self) -> bool:
        if self.quiet:
            return False
        self._prune(self._clock.monotonic())
        return len(self._grants) < self._max

    def acquire(self) -> bool:
        """Sprawdza zgodę i, jeśli jest, zapisuje ją w oknie 60 minut."""
        if not self.allowed():
            return False
        self._grants.append(self._clock.monotonic())
        return True
