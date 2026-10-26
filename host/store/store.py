"""Zapis zdarzeń, stanu i faktów w SQLite (docs/spec.md, sekcja 9).

Czas w bazie to UTC w stałym formacie ISO 8601, więc porównania tekstowe są poprawne.
Jedna instancja obsługuje jedną sesję hosta i może być używana z wielu wątków.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from host.core.clock import Clock, SystemClock
from host.core.continuity import Continuity
from host.core.events import HostEvent
from host.core.protocol import DecodedEvent
from host.store.models import Fact, SessionRow, Snapshot, StoredEvent
from host.store.schema import SchemaError, migrate

__all__ = ["Store", "StoreError", "SchemaError"]

FACT_SOURCES = ("stated", "inferred")


class StoreError(Exception):
    pass


def _to_db(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("czas bez strefy jest niedozwolony")
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _from_db(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _check_text(text: str) -> str:
    if not text.strip():
        raise ValueError("text nie może być pusty")
    return text


def _check_confidence(value: float) -> float:
    if isinstance(value, bool) or not 0 <= value <= 1:
        raise ValueError("confidence musi być liczbą z przedziału 0-1")
    return float(value)


class Store:
    def __init__(self, path: str | Path = ":memory:", clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            migrate(self._conn)
        except Exception:
            self._conn.close()
            raise
        self.session_id: int | None = None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def ping(self) -> None:
        """Rzuca wyjątek, gdy baza jest niedostępna."""
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    # --- sesje ---

    def begin_session(self) -> Continuity:
        """Otwiera sesję i zwraca ciągłość wyliczoną z poprzedniej."""
        now = self._clock.wall()
        with self._lock:
            if self.session_id is not None:
                raise StoreError("sesja jest już otwarta")
            with self._conn:
                prev = self._conn.execute(
                    "SELECT last_seen_at FROM sessions ORDER BY id DESC LIMIT 1"
                ).fetchone()
                prior = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                facts = self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                cur = self._conn.execute(
                    "INSERT INTO sessions(started_at, last_seen_at) VALUES (?, ?)",
                    (_to_db(now), _to_db(now)),
                )
            self.session_id = cur.lastrowid
        prev_dt = _from_db(prev["last_seen_at"]) if prev else None
        return Continuity.at_start(now, prev_dt, prior_sessions=prior, known_facts=facts)

    def end_session(self) -> None:
        """Czyste zamknięcie: ustawia ended_at i odświeża last_seen_at."""
        now = _to_db(self._clock.wall())
        with self._lock, self._conn:
            sid = self._require_session()
            self._conn.execute(
                "UPDATE sessions SET ended_at = ?, last_seen_at = MAX(last_seen_at, ?) WHERE id = ?",
                (now, now, sid),
            )

    def touch(self) -> None:
        """Odświeża last_seen_at bieżącej sesji (np. z tick), bez zapisu zdarzenia."""
        with self._lock, self._conn:
            self._touch(self._require_session(), _to_db(self._clock.wall()))

    def sessions(self) -> list[SessionRow]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
        return [
            SessionRow(
                r["id"],
                _from_db(r["started_at"]),
                _from_db(r["ended_at"]) if r["ended_at"] else None,
                _from_db(r["last_seen_at"]),
            )
            for r in rows
        ]

    def _require_session(self) -> int:
        if self.session_id is None:
            raise StoreError("brak otwartej sesji (wywołaj begin_session)")
        return self.session_id

    def _touch(self, sid: int, at: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET last_seen_at = MAX(last_seen_at, ?) WHERE id = ?", (at, sid)
        )

    # --- zdarzenia ---

    def append_event(
        self,
        *,
        at: datetime,
        source: str,
        type: str,
        data: dict[str, Any],
        ts_ms: int | None = None,
        boot_id: str | None = None,
    ) -> int:
        at_db = _to_db(at)
        payload = json.dumps(data, ensure_ascii=False)
        with self._lock, self._conn:
            sid = self._require_session()
            cur = self._conn.execute(
                "INSERT INTO events(session_id, at, source, type, ts_ms, boot_id, data)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, at_db, source, type, ts_ms, boot_id, payload),
            )
            self._touch(sid, at_db)
        return cur.lastrowid

    def append_host_event(self, ev: HostEvent) -> int:
        return self.append_event(at=ev.at, source="host", type=ev.type, data=ev.data)

    def append_body_event(
        self, decoded: DecodedEvent, *, at: datetime | None = None, boot_id: str | None = None
    ) -> int:
        """Zapisuje zdarzenie z ciała. `at` to czas zegarowy hosta (domyślnie teraz)."""
        return self.append_event(
            at=at or self._clock.wall(),
            source="body",
            type=decoded.type,
            data=decoded.body.model_dump(mode="json"),
            ts_ms=decoded.envelope.ts,
            boot_id=boot_id,
        )

    def events(
        self,
        *,
        types: Iterable[str] | None = None,
        since: datetime | None = None,
        after_id: int = 0,
        limit: int = 1000,
    ) -> list[StoredEvent]:
        """Zdarzenia rosnąco po id. `since` jest włączone, `after_id` wyłączone."""
        sql = "SELECT * FROM events WHERE id > ?"
        args: list[Any] = [after_id]
        if types is not None:
            types = list(types)
            if not types:
                return []
            sql += f" AND type IN ({','.join('?' * len(types))})"
            args += types
        if since is not None:
            sql += " AND at >= ?"
            args.append(_to_db(since))
        sql += " ORDER BY id LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [
            StoredEvent(
                r["id"], r["session_id"], _from_db(r["at"]), r["source"], r["type"],
                r["ts_ms"], r["boot_id"], json.loads(r["data"]),
            )
            for r in rows
        ]

    # --- migawki stanu ---

    def save_snapshot(self, self_state: dict[str, Any], body: dict[str, Any]) -> int:
        at = _to_db(self._clock.wall())
        with self._lock, self._conn:
            sid = self._require_session()
            cur = self._conn.execute(
                "INSERT INTO state_snapshots(session_id, at, self_json, body_json)"
                " VALUES (?, ?, ?, ?)",
                (
                    sid,
                    at,
                    json.dumps(self_state, ensure_ascii=False),
                    json.dumps(body, ensure_ascii=False),
                ),
            )
            self._touch(sid, at)
        return cur.lastrowid

    def snapshots(self, *, since: datetime | None = None, limit: int = 1000) -> list[Snapshot]:
        sql, args = "SELECT * FROM state_snapshots", []
        if since is not None:
            sql += " WHERE at >= ?"
            args.append(_to_db(since))
        sql += " ORDER BY id LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._snapshot(r) for r in rows]

    def latest_snapshot(self) -> Snapshot | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM state_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._snapshot(r) if r else None

    @staticmethod
    def _snapshot(r: sqlite3.Row) -> Snapshot:
        return Snapshot(
            r["id"], r["session_id"], _from_db(r["at"]),
            json.loads(r["self_json"]), json.loads(r["body_json"]),
        )

    # --- fakty ---

    def add_fact(self, text: str, source: str, confidence: float | None = None) -> Fact:
        if source not in FACT_SOURCES:
            raise ValueError(f"source musi być jednym z {FACT_SOURCES}")
        if confidence is None:
            if source == "inferred":
                raise ValueError("fakt 'inferred' wymaga confidence")
            confidence = 1.0
        confidence = _check_confidence(confidence)
        _check_text(text)
        now = _to_db(self._clock.wall())
        with self._lock, self._conn:
            sid = self._require_session()
            cur = self._conn.execute(
                "INSERT INTO facts(text, source, confidence, created_at, updated_at, session_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (text, source, confidence, now, now, sid),
            )
            return self._fact(cur.lastrowid)

    def facts(self, *, source: str | None = None) -> list[Fact]:
        sql, args = "SELECT * FROM facts", []
        if source is not None:
            sql += " WHERE source = ?"
            args.append(source)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id", args).fetchall()
        return [self._to_fact(r) for r in rows]

    def get_fact(self, fact_id: int) -> Fact | None:
        with self._lock:
            return self._fact(fact_id)

    def update_fact(
        self,
        fact_id: int,
        *,
        text: str | None = None,
        source: str | None = None,
        confidence: float | None = None,
    ) -> Fact | None:
        """Poprawka faktu. Zmiana tekstu bez podanego source/confidence oznacza wypowiedź
        użytkownika: source='stated', confidence=1. Zwraca None, gdy faktu nie ma."""
        if source is not None and source not in FACT_SOURCES:
            raise ValueError(f"source musi być jednym z {FACT_SOURCES}")
        if confidence is not None:
            confidence = _check_confidence(confidence)
        if text is not None:
            _check_text(text)
        with self._lock, self._conn:
            current = self._fact(fact_id)
            if current is None:
                return None
            new_text = current.text if text is None else text
            new_source, new_conf = current.source, current.confidence
            if text is not None and text != current.text:
                new_source, new_conf = "stated", 1.0
            if source is not None:
                new_source = source
            if confidence is not None:
                new_conf = confidence
            self._conn.execute(
                "UPDATE facts SET text = ?, source = ?, confidence = ?, updated_at = ?"
                " WHERE id = ?",
                (new_text, new_source, new_conf, _to_db(self._clock.wall()), fact_id),
            )
            return self._fact(fact_id)

    def delete_fact(self, fact_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        return cur.rowcount > 0

    def count_facts(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    def _fact(self, fact_id: int) -> Fact | None:
        r = self._conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return self._to_fact(r) if r else None

    @staticmethod
    def _to_fact(r: sqlite3.Row) -> Fact:
        return Fact(
            r["id"], r["text"], r["source"], r["confidence"],
            _from_db(r["created_at"]), _from_db(r["updated_at"]), r["session_id"],
        )
