"""Koperta, zdarzenia i akcje (docs/spec.md, sekcje 2, 3, 5, 11).

Nieznany typ, nieobsługiwana wersja lub uszkodzona ramka kończą się FrameRejected.
Wywołujący loguje i porzuca ramkę, połączenie zostaje.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROTOCOL_VERSION = 1

# Pola, których spec nie typuje dokładniej, przyjmują tekst lub liczbę.
Ident = str | int


class FrameRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Body(BaseModel):
    # Dodatkowe pola są zgodne wstecz (zmiana niezgodna podnosi `v`), więc je pomijamy.
    model_config = ConfigDict(extra="ignore")


class Envelope(_Body):
    v: int
    t: Literal["event", "action", "ack"]
    id: str
    ts: int
    type: str
    body: dict[str, Any] = Field(default_factory=dict)


# --- zdarzenia ciało -> host ---


class SystemBoot(_Body):
    boot_id: str
    fw: str
    reset_reason: str


class BodyHeartbeat(_Body):
    batt_pct: float = Field(ge=0, le=100)
    link_rssi: int
    frame_errors: int | None = None


class BodySensor(_Body):
    temp_c: float | None = None
    imu: Any = None
    presence: bool | None = None
    gps: Any = None


class BodyFault(_Body):
    sensor: str
    code: Ident


class BodyFaultClear(_Body):
    sensor: str


class AudioUtteranceStart(_Body):
    utt_id: Ident


class AudioUtteranceEnd(_Body):
    utt_id: Ident
    dur_ms: int = Field(ge=0)


class LocalCommand(_Body):
    cmd: str
    result: Any = None


class Button(_Body):
    id: Ident
    kind: Literal["press", "long"]


EVENT_MODELS: dict[str, type[_Body]] = {
    "system.boot": SystemBoot,
    "body.heartbeat": BodyHeartbeat,
    "body.sensor": BodySensor,
    "body.fault": BodyFault,
    "body.fault.clear": BodyFaultClear,
    "audio.utterance.start": AudioUtteranceStart,
    "audio.utterance.end": AudioUtteranceEnd,
    "local.command": LocalCommand,
    "button": Button,
}

# Zdarzenia, które liczą się jako interakcja użytkownika (spec, sekcja 8).
INTERACTION_EVENTS = frozenset({"audio.utterance.start", "local.command", "button"})


class AckBody(_Body):
    ref: str
    ok: bool
    err: str | None = None


@dataclass(frozen=True)
class DecodedEvent:
    envelope: Envelope
    body: _Body

    @property
    def type(self) -> str:
        return self.envelope.type


@dataclass(frozen=True)
class DecodedAck:
    envelope: Envelope
    body: AckBody


def decode_frame(raw: str | bytes) -> DecodedEvent | DecodedAck:
    """Dekoduje ramkę tekstową z ciała. Rzuca FrameRejected."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise FrameRejected("ramka nie jest UTF-8") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise FrameRejected("niepoprawny JSON") from None
    if not isinstance(data, dict):
        raise FrameRejected("koperta nie jest obiektem")
    if data.get("v") != PROTOCOL_VERSION:
        raise FrameRejected(f"nieobsługiwana wersja: {data.get('v')!r}")
    try:
        env = Envelope.model_validate(data)
    except ValidationError as e:
        raise FrameRejected(f"niepoprawna koperta: {_summary(e)}") from None

    if env.t == "action":
        raise FrameRejected("ciało nie wysyła akcji")
    if env.t == "ack":
        try:
            return DecodedAck(env, AckBody.model_validate(env.body))
        except ValidationError as e:
            raise FrameRejected(f"niepoprawny ack: {_summary(e)}") from None

    model = EVENT_MODELS.get(env.type)
    if model is None:
        raise FrameRejected(f"nieznany typ zdarzenia: {env.type!r}")
    try:
        return DecodedEvent(env, model.model_validate(env.body))
    except ValidationError as e:
        raise FrameRejected(f"niepoprawne body {env.type}: {_summary(e)}") from None


# --- akcje host -> ciało ---


class Speak(_Body):
    audio_id: Ident


class SpeakClip(_Body):
    clip: str


class Haptic(_Body):
    pattern: Literal["attention", "warn", "critical"]


class GpioSet(_Body):
    pin: int
    value: int


class ConfigSet(_Body):
    key: str
    value: Any


class Ping(_Body):
    pass


ACTION_MODELS: dict[str, type[_Body]] = {
    "speak": Speak,
    "speak.clip": SpeakClip,
    "haptic": Haptic,
    "gpio.set": GpioSet,
    "config.set": ConfigSet,
    "ping": Ping,
}


def encode_action(action_type: str, body: dict[str, Any], *, id: str, ts_ms: int) -> str:
    """Buduje ramkę akcji. Rzuca ValueError dla nieznanego typu lub niepoprawnego body."""
    model = ACTION_MODELS.get(action_type)
    if model is None:
        raise ValueError(f"nieznany typ akcji: {action_type!r}")
    try:
        validated = model.model_validate(body)
    except ValidationError as e:
        raise ValueError(f"niepoprawne body {action_type}: {_summary(e)}") from None
    frame = {
        "v": PROTOCOL_VERSION,
        "t": "action",
        "id": id,
        "ts": ts_ms,
        "type": action_type,
        "body": validated.model_dump(mode="json"),
    }
    return json.dumps(frame, separators=(",", ":"), ensure_ascii=False)


def _summary(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
