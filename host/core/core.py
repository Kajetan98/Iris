"""Fasada rdzenia: łączy body, drives, self i bramkę inicjatywy.

Działa bez LLM, STT i TTS. Wywołujący (host/api) dekoduje ramki, przekazuje zdarzenia
i okresowo woła tick(). Metody zwracają zdarzenia hosta do zapisu w host/store.

Urządzeń może być kilka (docs/spec.md, sekcja 1). Rdzeń prowadzi stan każdego z nich,
ale body, drives, interakcje i wycinek dla LLM wynikają tylko z aktywnego urządzenia.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from host.core.body import BodyState
from host.core.clock import Clock, SystemClock
from host.core.config import Config
from host.core.continuity import Continuity
from host.core.drives import DriveEngine
from host.core.events import HostEvent
from host.core.initiative import InitiativeGate
from host.core.protocol import (
    DEFAULT_DEVICE,
    INTERACTION_EVENTS,
    DecodedAck,
    DecodedEvent,
    DeviceCaps,
    FrameRejected,
    decode_frame,
)
from host.core.self_model import SelfModel

log = logging.getLogger(__name__)

SwitchBy = Literal["first", "rest", "device", "voice"]


class HostCore:
    def __init__(
        self,
        config: Config | None = None,
        clock: Clock | None = None,
        continuity: Continuity | None = None,
        active_device: str | None = None,
    ) -> None:
        self.config = config or Config()
        self.clock = clock or SystemClock()
        now = self.clock.wall()
        self.continuity = continuity or Continuity.at_start(now, None)
        self.drives = DriveEngine(self.config, self.clock)
        self.initiative = InitiativeGate(self.config.initiative, self.clock)
        self.self_model = SelfModel(self.config, self.clock, self.continuity)
        self.last_interaction = now
        self.active_device = active_device
        self._bodies: dict[str, BodyState] = {}
        # Stan sprzed pierwszego urządzenia; przejmuje go pierwsze aktywne (spec, sekcja 7).
        self._unassigned: BodyState | None = None
        if active_device is None:
            self._unassigned = BodyState(self.config, self.clock)
        else:
            self._bodies[active_device] = BodyState(self.config, self.clock, active_device)

    @property
    def body(self) -> BodyState:
        """Stan aktywnego urządzenia (albo stan sprzed pierwszego urządzenia)."""
        if self.active_device is None:
            assert self._unassigned is not None
            return self._unassigned
        return self._bodies[self.active_device]

    def start(self) -> list[HostEvent]:
        data = {**self.continuity.snapshot(), "last_downtime_s": self.continuity.last_downtime_s}
        return [HostEvent(type="host.start", at=self.clock.wall(), data=data)]

    def stop(self) -> list[HostEvent]:
        return [HostEvent(type="host.stop", at=self.clock.wall())]

    # --- urządzenia ---

    def connect(self, device: str) -> list[HostEvent]:
        """Urządzenie się połączyło. Pierwsze w ogóle staje się aktywne."""
        if self.active_device is None:
            return self._adopt(device, "first")
        if device not in self._bodies:
            self._bodies[device] = BodyState(self.config, self.clock, device)
        return []

    def activate(self, device: str, by: SwitchBy) -> list[HostEvent]:
        """Ręczne przełączenie aktywnego urządzenia (REST lub device.activate)."""
        if device == self.active_device:
            return []
        if self.active_device is None:
            return self._adopt(device, by)
        previous = self.active_device
        events = self.drives.end_body_drives(previous)
        if device not in self._bodies:
            self._bodies[device] = BodyState(self.config, self.clock, device)
        self.active_device = device
        events.append(self._switch_event(previous, device, by))
        events += self.drives.update(self.body, self.last_interaction)
        return events

    def _adopt(self, device: str, by: SwitchBy) -> list[HostEvent]:
        body = self._bodies.get(device)
        if body is None:
            assert self._unassigned is not None
            body, body.device = self._unassigned, device
            self._bodies[device] = body
        self._unassigned = None
        self.active_device = device
        return [self._switch_event(None, device, by)]

    def _switch_event(self, previous: str | None, device: str, by: SwitchBy) -> HostEvent:
        log.info("aktywne urządzenie: %s -> %s (%s)", previous, device, by)
        return HostEvent(
            type="device.switch",
            at=self.clock.wall(),
            data={"from": previous, "to": device, "by": by},
            device=device,
        )

    def device_state(self, device: str) -> dict[str, Any] | None:
        """Stan urządzenia w bieżącej sesji albo None, gdy się nie łączyło."""
        body = self._bodies.get(device)
        return None if body is None else body.snapshot()

    def device_caps(self, device: str | None) -> DeviceCaps | None:
        """Ostatnie device.caps urządzenia w tej sesji albo None (możliwości HERMES)."""
        body = None if device is None else self._bodies.get(device)
        return None if body is None else body.caps

    def known_devices(self) -> list[str]:
        return sorted(self._bodies)

    # --- zdarzenia ---

    def handle_frame(self, raw: str | bytes, device: str = DEFAULT_DEVICE) -> list[HostEvent]:
        """Dekoduje i przetwarza ramkę. Ramka odrzucona jest logowana i pomijana."""
        try:
            decoded = decode_frame(raw)
        except FrameRejected as e:
            log.warning("ramka odrzucona: %s", e.reason)
            return []
        if isinstance(decoded, DecodedAck):
            return []  # potwierdzenia akcji obsługuje warstwa api
        return self.handle_event(decoded, device)

    def handle_event(self, ev: DecodedEvent, device: str = DEFAULT_DEVICE) -> list[HostEvent]:
        events = self.tick()  # utrata łącza przed tą ramką ma być zapisana przed link.restored
        if device not in self._bodies:
            events += self.connect(device)
        events += self._bodies[device].apply(ev)
        if device != self.active_device:
            if ev.type != "device.activate":
                return events  # urządzenie w spoczynku: tylko jego własny stan
            events += self.activate(device, "device")
        if ev.type in INTERACTION_EVENTS:
            self.last_interaction = self.clock.wall()
        events += self.drives.update(self.body, self.last_interaction)
        return events

    def tick(self) -> list[HostEvent]:
        events: list[HostEvent] = []
        if self._unassigned is not None:
            events += self._unassigned.tick()
        for device in sorted(self._bodies):
            events += self._bodies[device].tick()
        events += self.drives.update(self.body, self.last_interaction)
        return events

    # --- stan ---

    def self_snapshot(self) -> dict[str, Any]:
        """Odpowiedź dla GET /self."""
        return {
            "self": self.self_model.snapshot(self.drives.active(), self.active_device),
            "body": self.body.snapshot(),
        }

    def prompt_slice(self) -> dict[str, Any]:
        return self.self_model.prompt_slice(self.drives.active(), self.body, self.active_device)

    def rollback_prompt_slice(self) -> bool:
        return self.self_model.rollback_prompt_slice()

    def note_interaction(self) -> list[HostEvent]:
        """Interakcja spoza ramek ciała (np. POST /chat). Kończy long_silence."""
        self.last_interaction = self.clock.wall()
        return self.tick()

    def set_known_facts(self, n: int) -> None:
        self.continuity.known_facts = n
