from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class SessionRow:
    id: int
    started_at: datetime
    ended_at: datetime | None
    last_seen_at: datetime


@dataclass(frozen=True)
class StoredEvent:
    id: int
    session_id: int
    at: datetime
    source: str  # body | host
    type: str
    ts_ms: int | None
    boot_id: str | None
    data: dict[str, Any]


@dataclass(frozen=True)
class Snapshot:
    id: int
    session_id: int
    at: datetime
    self_state: dict[str, Any]
    body: dict[str, Any]


@dataclass(frozen=True)
class Fact:
    id: int
    text: str
    source: str  # stated | inferred
    confidence: float
    created_at: datetime
    updated_at: datetime
    session_id: int | None
