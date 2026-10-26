"""Fasada rdzenia: łączy body, drives, self i bramkę inicjatywy.

Działa bez LLM, STT i TTS. Wywołujący (host/api) dekoduje ramki, przekazuje zdarzenia
i okresowo woła tick(). Metody zwracają zdarzenia hosta do zapisu w host/store.
"""

from __future__ import annotations

import logging
from typing import Any

from host.core.body import BodyState
from host.core.clock import Clock, SystemClock
from host.core.config import Config
from host.core.continuity import Continuity
from host.core.drives import DriveEngine
from host.core.events import HostEvent
from host.core.initiative import InitiativeGate
from host.core.protocol import (
    INTERACTION_EVENTS,
    DecodedAck,
    DecodedEvent,
    FrameRejected,
    decode_frame,
)
from host.core.self_model import SelfModel

log = logging.getLogger(__name__)


class HostCore:
    def __init__(
        self,
        config: Config | None = None,
        clock: Clock | None = None,
        continuity: Continuity | None = None,
    ) -> None:
        self.config = config or Config()
        self.clock = clock or SystemClock()
        now = self.clock.wall()
        self.continuity = continuity or Continuity.at_start(now, None)
        self.body = BodyState(self.config, self.clock)
        self.drives = DriveEngine(self.config, self.clock)
        self.initiative = InitiativeGate(self.config.initiative, self.clock)
        self.self_model = SelfModel(self.config, self.clock, self.continuity)
        self.last_interaction = now

    def start(self) -> list[HostEvent]:
        data = {**self.continuity.snapshot(), "last_downtime_s": self.continuity.last_downtime_s}
        return [HostEvent(type="host.start", at=self.clock.wall(), data=data)]

    def stop(self) -> list[HostEvent]:
        return [HostEvent(type="host.stop", at=self.clock.wall())]

    def handle_frame(self, raw: str | bytes) -> list[HostEvent]:
        """Dekoduje i przetwarza ramkę. Ramka odrzucona jest logowana i pomijana."""
        try:
            decoded = decode_frame(raw)
        except FrameRejected as e:
            log.warning("ramka odrzucona: %s", e.reason)
            return []
        if isinstance(decoded, DecodedAck):
            return []  # potwierdzenia akcji obsługuje warstwa api
        return self.handle_event(decoded)

    def handle_event(self, ev: DecodedEvent) -> list[HostEvent]:
        events = self.tick()  # utrata łącza przed tą ramką ma być zapisana przed link.restored
        events += self.body.apply(ev)
        if ev.type in INTERACTION_EVENTS:
            self.last_interaction = self.clock.wall()
        events += self.drives.update(self.body, self.last_interaction)
        return events

    def tick(self) -> list[HostEvent]:
        events = self.body.tick()
        events += self.drives.update(self.body, self.last_interaction)
        return events

    def self_snapshot(self) -> dict[str, Any]:
        """Odpowiedź dla GET /self."""
        return {
            "self": self.self_model.snapshot(self.drives.active()),
            "body": self.body.snapshot(),
        }

    def prompt_slice(self) -> dict[str, Any]:
        return self.self_model.prompt_slice(self.drives.active(), self.body)

    def set_known_facts(self, n: int) -> None:
        self.continuity.known_facts = n
