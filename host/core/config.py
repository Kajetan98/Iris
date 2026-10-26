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


class Config(_Model):
    name: str = "IRIS"
    version: str = "0.1.0"
    host_name: str = Field(default_factory=platform.node)
    sensors: list[str] = ["mic", "imu", "temp", "presence", "gps"]
    link: LinkConfig = LinkConfig()
    drives: DrivesConfig = DrivesConfig()
    initiative: InitiativeConfig = InitiativeConfig()
    prompt: PromptConfig = PromptConfig()

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path, "rb") as f:
            return cls.model_validate(tomllib.load(f))
