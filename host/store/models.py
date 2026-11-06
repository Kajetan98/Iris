from __future__ import annotations

from dataclasses import dataclass, field
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
    device: str | None = None


@dataclass(frozen=True)
class DeviceRow:
    id: str
    first_seen_at: datetime
    last_seen_at: datetime
    active: bool


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
    reinforced: int = 0  # ile razy wniosek się powtórzył (spec, sekcja 9)
    asked_at: datetime | None = None  # kiedy host ostatnio o niego pytał


@dataclass(frozen=True)
class Concept:
    """Pojęcie w grafie pamięci (spec, sekcja 9). `strength` to siła w chwili `last_used`."""

    key: str
    label: str
    strength: float
    last_used: datetime
    hits: int
    created_at: datetime
    domains: dict[str, int] = field(default_factory=dict)  # ile razy pojęcie padło w każdej domenie


@dataclass(frozen=True)
class Link:
    """Połączenie pojęć z jednej wypowiedzi; `a` < `b`."""

    a: str
    b: str
    strength: float
    last_used: datetime
