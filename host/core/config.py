"""Konfiguracja rdzenia. Progi i limity ze specyfikacji (docs/spec.md) to wartości początkowe."""

from __future__ import annotations

import platform
import tomllib
from datetime import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ThresholdRule(_Model):
    """Reguła z histerezą. `below`: wejście przy < enter, wyjście przy >= exit.
    `above`: wejście przy > enter, wyjście przy < exit."""

    direction: Literal["below", "above"]
    enter: float
    exit: float
    enter_hold_s: float = Field(ge=0)
    exit_hold_s: float = Field(ge=0)

    @model_validator(mode="after")
    def _exit_on_return_side(self) -> "ThresholdRule":
        if self.direction == "below" and self.exit < self.enter:
            raise ValueError("dla 'below' próg wyjścia musi być >= progowi wejścia")
        if self.direction == "above" and self.exit > self.enter:
            raise ValueError("dla 'above' próg wyjścia musi być <= progowi wejścia")
        return self


class LinkConfig(_Model):
    timeout_s: float = Field(5.0, gt=0)
    recover_s: float = Field(5.0, ge=0)


class ActiveHours(_Model):
    start: time = time(8, 0)
    end: time = time(22, 0)

    @model_validator(mode="after")
    def _non_empty(self) -> "ActiveHours":
        if self.start == self.end:
            raise ValueError("start i end nie mogą być równe")
        return self

    def contains(self, t: time) -> bool:
        t = t.replace(tzinfo=None)
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end


class DrivesConfig(_Model):
    battery_low: ThresholdRule = ThresholdRule(
        direction="below", enter=20, exit=25, enter_hold_s=10, exit_hold_s=30
    )
    battery_critical: ThresholdRule = ThresholdRule(
        direction="below", enter=10, exit=15, enter_hold_s=10, exit_hold_s=30
    )
    temp_high: ThresholdRule = ThresholdRule(
        direction="above", enter=40, exit=37, enter_hold_s=30, exit_hold_s=60
    )
    long_silence_after_s: float = Field(4 * 3600, gt=0)
    active_hours: ActiveHours = ActiveHours()


class InitiativeConfig(_Model):
    max_per_hour: int = Field(2, ge=0)
    quiet: bool = False


class PromptConfig(_Model):
    tolerance: dict[str, float] = {"batt_pct": 5, "temp_c": 2, "rssi": 10}
    min_downtime_s: float = Field(60, ge=0)


class StoreConfig(_Model):
    path: str = "iris.db"


class ApiConfig(_Model):
    tick_interval_s: float = Field(1.0, gt=0)
    touch_every_s: float = Field(10.0, gt=0)
    snapshot_every_s: float = Field(300.0, gt=0)
    store_heartbeats: bool = False
    ack_timeout_s: float = Field(2.0, gt=0)


class LLMConfig(_Model):
    engine: Literal["none", "ollama"] = "none"
    model: str | None = None
    base_url: str = "http://localhost:11434"
    max_tokens: int = Field(300, gt=0)
    temperature: float | None = Field(None, ge=0, le=2)
    timeout_s: float = Field(30.0, gt=0)
    retries: int = Field(1, ge=0, le=5)
    history_turns: int = Field(10, ge=0)
    send_facts: Literal["always", "local_only", "never"] = "local_only"
    max_facts: int = Field(30, ge=0)
    min_inferred_confidence: float = Field(0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _model_required(self) -> "LLMConfig":
        if self.engine != "none" and not self.model:
            raise ValueError("engine wymaga ustawienia model")
        return self


class STTConfig(_Model):
    engine: Literal["none", "faster-whisper"] = "none"
    model: str | None = None  # nazwa lub repozytorium HF (pobierane) albo ścieżka absolutna / zaczynająca się od "." lub "~"
    model_dir: str | None = None
    local_files_only: bool = False
    language: str = "pl"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = Field(1, ge=1, le=10)
    vad_filter: bool = True
    cpu_threads: int = Field(0, ge=0)
    min_text_chars: int = Field(2, ge=0)

    @model_validator(mode="after")
    def _model_required(self) -> "STTConfig":
        if self.engine != "none" and not self.model:
            raise ValueError("engine wymaga ustawienia model")
        return self


class TTSConfig(_Model):
    engine: Literal["none", "piper"] = "none"
    voice: str | None = None  # ścieżka do pliku .onnx (obok musi leżeć .onnx.json)
    length_scale: float | None = Field(None, gt=0)
    speaker_id: int | None = Field(None, ge=0)
    volume: float | None = Field(None, gt=0)
    max_chars: int = Field(1000, gt=0)

    @model_validator(mode="after")
    def _voice_required(self) -> "TTSConfig":
        if self.engine != "none" and not self.voice:
            raise ValueError("engine wymaga ustawienia voice")
        return self


OPUS_RATES = (8000, 12000, 16000, 24000, 48000)
OPUS_FRAME_MS = (2.5, 5.0, 10.0, 20.0, 40.0, 60.0)


class AudioConfig(_Model):
    playback_rate_hz: int = 16000
    frame_ms: float = 20.0
    bitrate_bps: int = Field(24000, ge=6000, le=510000)
    max_utterance_s: float = Field(30.0, gt=0)
    min_utterance_ms: float = Field(300.0, ge=0)
    max_speech_s: float = Field(15.0, gt=0)
    max_pending_turns: int = Field(3, ge=1)
    error_clip: str | None = None  # nazwa klipu zapisanego w ciele, odtwarzanego po nieudanej turze

    @model_validator(mode="after")
    def _opus_parameters(self) -> "AudioConfig":
        if self.playback_rate_hz not in OPUS_RATES:
            raise ValueError(f"playback_rate_hz musi być jedną z {OPUS_RATES}")
        if self.frame_ms not in OPUS_FRAME_MS:
            raise ValueError(f"frame_ms musi być jedną z {OPUS_FRAME_MS}")
        return self


class BrainConfig(_Model):
    llm: LLMConfig = LLMConfig()
    stt: STTConfig = STTConfig()
    tts: TTSConfig = TTSConfig()
    audio: AudioConfig = AudioConfig()


class Config(_Model):
    name: str = "IRIS"
    version: str = "0.1.0"
    host_name: str = Field(default_factory=platform.node)
    sensors: list[str] = ["mic", "imu", "temp", "presence", "gps"]
    link: LinkConfig = LinkConfig()
    drives: DrivesConfig = DrivesConfig()
    initiative: InitiativeConfig = InitiativeConfig()
    prompt: PromptConfig = PromptConfig()
    store: StoreConfig = StoreConfig()
    api: ApiConfig = ApiConfig()
    brain: BrainConfig = BrainConfig()

    @model_validator(mode="after")
    def _voice_engines_together(self) -> "Config":
        if (self.brain.stt.engine != "none") != (self.brain.tts.engine != "none"):
            raise ValueError("brain.stt i brain.tts trzeba włączać razem")
        return self

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path, "rb") as f:
            return cls.model_validate(tomllib.load(f))
