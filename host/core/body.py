"""Stan ciała budowany ze zdarzeń (docs/spec.md, sekcja 7)."""

from __future__ import annotations

from typing import Any

from host.core.clock import Clock
from host.core.config import Config
from host.core.events import HostEvent
from host.core.protocol import (
    BodyFault,
    BodyFaultClear,
    BodyHeartbeat,
    BodySensor,
    DecodedEvent,
    SystemBoot,
)


class BodyState:
    def __init__(self, config: Config, clock: Clock) -> None:
        self._cfg = config
        self._clock = clock
        self._started = clock.monotonic()
        self.link: str = "unknown"  # unknown | ok | lost
        self.batt_pct: float | None = None
        self.rssi: int | None = None
        self.temp_c: float | None = None
        self.faults: dict[str, Any] = {}
        self.boot_id: str | None = None
        self.fw: str | None = None
        self.reset_reason: str | None = None
        self.frame_errors: int | None = None
        self._last_heartbeat: float | None = None
        self._last_frame: float | None = None

    def apply(self, ev: DecodedEvent) -> list[HostEvent]:
        """Uwzględnia zdarzenie z ciała. Zwraca zdarzenia hosta (link.restored)."""
        self._last_frame = self._clock.monotonic()
        body = ev.body
        out: list[HostEvent] = []
        if isinstance(body, BodyHeartbeat):
            out += self._on_heartbeat(body)
        elif isinstance(body, BodySensor):
            if body.temp_c is not None:
                self.temp_c = body.temp_c
        elif isinstance(body, BodyFault):
            self.faults[body.sensor] = body.code
        elif isinstance(body, BodyFaultClear):
            self.faults.pop(body.sensor, None)
        elif isinstance(body, SystemBoot):
            self.boot_id, self.fw, self.reset_reason = body.boot_id, body.fw, body.reset_reason
            self.faults.clear()
        return out

    def tick(self) -> list[HostEvent]:
        """Wykrywa utratę łącza. Odniesieniem przed pierwszym heartbeatem jest start hosta."""
        if self.link == "lost":
            return []
        ref = self._last_heartbeat if self._last_heartbeat is not None else self._started
        silent_for = self._clock.monotonic() - ref
        if silent_for > self._cfg.link.timeout_s:
            self.link = "lost"
            return [HostEvent(type="link.lost", at=self._clock.wall(), data={"silent_s": silent_for})]
        return []

    def _on_heartbeat(self, hb: BodyHeartbeat) -> list[HostEvent]:
        self._last_heartbeat = self._clock.monotonic()
        self.batt_pct = hb.batt_pct
        self.rssi = hb.link_rssi
        if hb.frame_errors is not None:
            self.frame_errors = hb.frame_errors
        was_lost = self.link == "lost"
        self.link = "ok"
        if was_lost:
            return [HostEvent(type="link.restored", at=self._clock.wall())]
        return []

    @property
    def sensors_down(self) -> list[str]:
        known = [s for s in self._cfg.sensors if s in self.faults]
        extra = sorted(s for s in self.faults if s not in self._cfg.sensors)
        return known + extra

    @property
    def sensors_ok(self) -> list[str]:
        return [s for s in self._cfg.sensors if s not in self.faults]

    def snapshot(self) -> dict[str, Any]:
        last_seen_ms = (
            None
            if self._last_frame is None
            else int((self._clock.monotonic() - self._last_frame) * 1000)
        )
        return {
            "batt_pct": self.batt_pct,
            "link": self.link,
            "rssi": self.rssi,
            "temp_c": self.temp_c,
            "sensors_ok": self.sensors_ok,
            "sensors_down": self.sensors_down,
            "last_seen_ms": last_seen_ms,
        }
