"""Model `self` i wycinek stanu dla promptu LLM (docs/spec.md, sekcja 7).

Opisy stanu pochodzą z danych. Moduł niczego nie przypisuje programowi poza tym, co zmierzone.
"""

from __future__ import annotations

from typing import Any

from host.core.body import BodyState
from host.core.clock import Clock
from host.core.config import Config
from host.core.continuity import Continuity


def mode_for(drives: list[str]) -> str:
    if "link_lost" in drives:
        return "offline"
    if "battery_low" in drives or "battery_critical" in drives:
        return "saving"
    return "nominal"


class SelfModel:
    def __init__(self, config: Config, clock: Clock, continuity: Continuity) -> None:
        self._cfg = config
        self._clock = clock
        self._started = clock.monotonic()
        self.continuity = continuity
        # Odniesienie dla "zmian od ostatniego zapytania", osobne dla każdego pola.
        self._reported: dict[str, Any] = {}
        self._downtime_reported = False
        self._undo: tuple[dict[str, Any], bool] | None = None

    def snapshot(self, drives: list[str]) -> dict[str, Any]:
        return {
            "name": self._cfg.name,
            "version": self._cfg.version,
            "host": self._cfg.host_name,
            "mode": mode_for(drives),
            "uptime_s": int(self._clock.monotonic() - self._started),
            "last_restart": self.continuity.last_restart.isoformat(),
            "last_downtime_s": self.continuity.last_downtime_s,
            "drives": drives,
            "continuity": self.continuity.snapshot(),
        }

    def prompt_slice(self, drives: list[str], body: BodyState) -> dict[str, Any]:
        """Wycinek do promptu. Zapamiętuje raportowane wartości jako nowe odniesienie.

        Poprzednie odniesienie można przywrócić przez rollback_prompt_slice()."""
        self._undo = (dict(self._reported), self._downtime_reported)
        out: dict[str, Any] = {"mode": mode_for(drives), "drives": drives}
        changes = self._body_changes(body.snapshot())
        if changes:
            out["body_changes"] = changes
        # Przerwa jest informacją o starcie sesji, więc trafia do pierwszego wycinka.
        if not self._downtime_reported:
            self._downtime_reported = True
            if self.continuity.last_downtime_s >= self._cfg.prompt.min_downtime_s:
                out["last_downtime_s"] = self.continuity.last_downtime_s
        return out

    def rollback_prompt_slice(self) -> bool:
        """Cofa ostatnio pobrany wycinek (np. po nieudanym zapytaniu do LLM).

        Zwraca fałsz, gdy nie ma czego cofać. Cofnąć można tylko jeden wycinek."""
        if self._undo is None:
            return False
        self._reported, self._downtime_reported = self._undo
        self._undo = None
        return True

    def _body_changes(self, snap: dict[str, Any]) -> dict[str, dict[str, Any]]:
        tolerance = self._cfg.prompt.tolerance
        changes: dict[str, dict[str, Any]] = {}
        for key in ("batt_pct", "link", "rssi", "temp_c", "sensors_ok", "sensors_down"):
            new = snap[key]
            old = self._reported.get(key)
            if not _differs(old, new, tolerance.get(key)):
                continue
            changes[key] = {"from": old, "to": new}
            self._reported[key] = new
        return changes


def _differs(old: Any, new: Any, tolerance: float | None) -> bool:
    if old == new:
        return False
    if tolerance is not None and isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return abs(new - old) > tolerance
    return True
