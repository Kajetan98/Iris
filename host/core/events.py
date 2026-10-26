"""Zdarzenia wewnętrzne hosta (docs/spec.md, sekcja 4)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

HostEventType = Literal[
    "link.lost",
    "link.restored",
    "drive.enter",
    "drive.exit",
    "host.start",
    "host.stop",
    "action.sent",
    "action.ack",
]


class HostEvent(BaseModel):
    type: HostEventType
    at: datetime
    data: dict[str, Any] = Field(default_factory=dict)
