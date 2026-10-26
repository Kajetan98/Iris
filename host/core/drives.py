"""Potrzeby (drives): deterministyczne reguły z histerezą (docs/spec.md, sekcja 8).

Nie używa LLM. Czas pochodzi z wstrzykniętego zegara.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from host.core.body import BodyState
from host.core.clock import Clock
from host.core.config import Config, ThresholdRule
from host.core.events import HostEvent

DRIVE_ORDER = ("battery_low", "battery_critical", "link_lost", "temp_high", "long_silence")


class _ThresholdDrive:
    def __init__(self, rule: ThresholdRule) -> None:
        self._rule = rule
        self.active = False
        self._since: float | None = None

    def _condition(self, value: float) -> bool:
        r = self._rule
        if r.direction == "below":
            return value >= r.exit if self.active else value < r.enter
        return value < r.exit if self.active else value > r.enter

    def update(self, now: float, value: float | None) -> str | None:
        """Zwraca "enter", "exit" albo None. value=None oznacza dane niedostępne."""
        if value is None or not self._condition(value):
            self._since = None
            return None
        if self._since is None:
            self._since = now
        hold = self._rule.exit_hold_s if self.active else self._rule.enter_hold_s
        if now - self._since < hold:
            return None
        self.active = not self.active
        self._since = None
        return "exit" if not self.active else "enter"


class _LinkDrive:
    def __init__(self, recover_s: float) -> None:
        self._recover_s = recover_s
        self.active = False
        self._ok_since: float | None = None

    def update(self, now: float, link: str) -> str | None:
        if not self.active:
            if link == "lost":
                self.active = True
                self._ok_since = None
                return "enter"
            return None
        if link != "ok":
            self._ok_since = None
            return None
        if self._ok_since is None:
            self._ok_since = now
        if now - self._ok_since >= self._recover_s:
            self.active = False
            self._ok_since = None
            return "exit"
        return None


class DriveEngine:
    def __init__(self, config: Config, clock: Clock) -> None:
        self._cfg = config
        self._clock = clock
        d = config.drives
        self._battery_low = _ThresholdDrive(d.battery_low)
        self._battery_critical = _ThresholdDrive(d.battery_critical)
        self._temp_high = _ThresholdDrive(d.temp_high)
        self._link_lost = _LinkDrive(config.link.recover_s)
        self._long_silence = False

    def active(self) -> list[str]:
        flags = {
            "battery_low": self._battery_low.active,
            "battery_critical": self._battery_critical.active,
            "link_lost": self._link_lost.active,
            "temp_high": self._temp_high.active,
            "long_silence": self._long_silence,
        }
        return [name for name in DRIVE_ORDER if flags[name]]

    def update(self, body: BodyState, last_interaction: datetime) -> list[HostEvent]:
        now = self._clock.monotonic()
        wall = self._clock.wall()
        link_ok = body.link == "ok"
        batt = body.batt_pct if link_ok else None
        temp = body.temp_c if link_ok and "temp" not in body.faults else None

        results: dict[str, tuple[str | None, dict[str, Any]]] = {
            "battery_low": (self._battery_low.update(now, batt), {"value": batt}),
            "battery_critical": (self._battery_critical.update(now, batt), {"value": batt}),
            "link_lost": (self._link_lost.update(now, body.link), {}),
            "temp_high": (self._temp_high.update(now, temp), {"value": temp}),
            "long_silence": self._update_long_silence(wall, last_interaction),
        }
        events = []
        for name in DRIVE_ORDER:
            change, data = results[name]
            if change is not None:
                events.append(HostEvent(type=f"drive.{change}", at=wall, data={"drive": name, **data}))
        return events

    def _update_long_silence(
        self, wall: datetime, last_interaction: datetime
    ) -> tuple[str | None, dict[str, Any]]:
        silence_s = (wall - last_interaction).total_seconds()
        after_s = self._cfg.drives.long_silence_after_s
        data = {"silence_s": silence_s}
        if not self._long_silence:
            in_hours = self._cfg.drives.active_hours.contains(wall.time())
            if silence_s > after_s and in_hours:
                self._long_silence = True
                return "enter", data
            return None, data
        if silence_s <= after_s:
            self._long_silence = False
            return "exit", data
        return None, data
