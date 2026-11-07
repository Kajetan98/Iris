"""Zapis zdarzeń, stanu i pamięci (graf pojęć) w SQLite (docs/spec.md, sekcja 9).

Czas w bazie to UTC w stałym formacie ISO 8601, więc porównania tekstowe są poprawne.
Jedna instancja obsługuje jedną sesję hosta i może być używana z wielu wątków.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from host.core.clock import Clock, SystemClock
from host.core.config import MemoryConfig
from host.core.continuity import Continuity
from host.core.events import HostEvent
from host.core.protocol import DecodedEvent
from host.store.models import Concept, DeviceRow, Link, SessionRow, Snapshot, StoredEvent
from host.store.schema import SchemaError, migrate

__all__ = ["Store", "StoreError", "SchemaError"]

class StoreError(Exception):
    pass


def _to_db(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("czas bez strefy jest niedozwolony")
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _from_db(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Store:
    def __init__(
        self, path: str | Path = ":memory:", clock: Clock | None = None, memory: MemoryConfig | None = None
    ) -> None:
        self._clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            cfg = memory or MemoryConfig()
            migrate(self._conn, on_v4=_migrate_facts(cfg), on_v5=_rekey(cfg))
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
                concepts = self._conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0]
                cur = self._conn.execute(
                    "INSERT INTO sessions(started_at, last_seen_at) VALUES (?, ?)",
                    (_to_db(now), _to_db(now)),
                )
            self.session_id = cur.lastrowid
        prev_dt = _from_db(prev["last_seen_at"]) if prev else None
        return Continuity.at_start(now, prev_dt, prior_sessions=prior, known_concepts=concepts)

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
        device: str | None = None,
    ) -> int:
        at_db = _to_db(at)
        payload = json.dumps(data, ensure_ascii=False)
        with self._lock, self._conn:
            sid = self._require_session()
            cur = self._conn.execute(
                "INSERT INTO events(session_id, at, source, type, ts_ms, boot_id, data, device_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, at_db, source, type, ts_ms, boot_id, payload, device),
            )
            self._touch(sid, at_db)
        return cur.lastrowid

    def append_host_event(self, ev: HostEvent) -> int:
        return self.append_event(at=ev.at, source="host", type=ev.type, data=ev.data, device=ev.device)

    def append_body_event(
        self,
        decoded: DecodedEvent,
        *,
        at: datetime | None = None,
        boot_id: str | None = None,
        device: str | None = None,
    ) -> int:
        """Zapisuje zdarzenie z ciała. `at` to czas zegarowy hosta (domyślnie teraz)."""
        return self.append_event(
            at=at or self._clock.wall(),
            source="body",
            type=decoded.type,
            data=decoded.body.model_dump(mode="json"),
            ts_ms=decoded.envelope.ts,
            boot_id=boot_id,
            device=device,
        )

    def events(
        self,
        *,
        types: Iterable[str] | None = None,
        since: datetime | None = None,
        after_id: int = 0,
        limit: int = 1000,
        device: str | None = None,
    ) -> list[StoredEvent]:
        """Zdarzenia rosnąco po id. `since` jest włączone, `after_id` wyłączone."""
        sql = "SELECT * FROM events WHERE id > ?"
        args: list[Any] = [after_id]
        if device is not None:
            sql += " AND device_id = ?"
            args.append(device)
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
                r["ts_ms"], r["boot_id"], json.loads(r["data"]), r["device_id"],
            )
            for r in rows
        ]

    # --- urządzenia ---

    def see_device(self, device: str) -> None:
        """Połączenie lub rozłączenie urządzenia: dopisuje je albo odświeża last_seen_at."""
        now = _to_db(self._clock.wall())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO devices(id, first_seen_at, last_seen_at) VALUES (?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET last_seen_at = MAX(last_seen_at, excluded.last_seen_at)",
                (device, now, now),
            )

    def touch_devices(self, devices: Iterable[str]) -> None:
        now = _to_db(self._clock.wall())
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE devices SET last_seen_at = MAX(last_seen_at, ?) WHERE id = ?",
                [(now, d) for d in devices],
            )

    def devices(self) -> list[DeviceRow]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM devices ORDER BY id").fetchall()
        return [
            DeviceRow(r["id"], _from_db(r["first_seen_at"]), _from_db(r["last_seen_at"]), bool(r["active"]))
            for r in rows
        ]

    def device_known(self, device: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM devices WHERE id = ?", (device,)).fetchone() is not None

    def active_device(self) -> str | None:
        with self._lock:
            r = self._conn.execute("SELECT id FROM devices WHERE active = 1").fetchone()
        return r["id"] if r else None

    def set_active_device(self, device: str) -> None:
        """Zapamiętuje aktywne urządzenie. KeyError, gdy urządzenie nie jest znane."""
        with self._lock, self._conn:
            if not self._conn.execute("SELECT 1 FROM devices WHERE id = ?", (device,)).fetchone():
                raise KeyError(device)
            self._conn.execute("UPDATE devices SET active = 0 WHERE active = 1 AND id != ?", (device,))
            self._conn.execute("UPDATE devices SET active = 1 WHERE id = ?", (device,))

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

    # --- pamięć: graf pojęć (spec, sekcja 9) ---

    def concepts(self, keys: Iterable[str] | None = None) -> dict[str, Concept]:
        """Pojęcia o podanych kluczach (wszystkie, gdy `keys` to None)."""
        with self._lock:
            return _load_concepts(self._conn, None if keys is None else list(keys))

    def links_touching(self, keys: Iterable[str]) -> list[Link]:
        """Połączenia, których jednym końcem jest któryś z kluczy."""
        keys = list(dict.fromkeys(keys))
        if not keys:
            return []
        marks = ",".join("?" * len(keys))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM concept_links WHERE a IN ({marks}) OR b IN ({marks})", keys + keys
            ).fetchall()
        return [_to_link(r) for r in rows]

    def links_between(self, keys: Iterable[str]) -> dict[tuple[str, str], Link]:
        with self._lock:
            return _load_links_between(self._conn, list(keys))

    def all_links(self) -> list[Link]:
        with self._lock:
            return [_to_link(r) for r in self._conn.execute("SELECT * FROM concept_links").fetchall()]

    def save_memory(
        self,
        concepts: Iterable[Concept] = (),
        links: Iterable[Link] = (),
        *,
        delete: Iterable[str] = (),
        delete_links: Iterable[tuple[str, str]] = (),
        forgotten: int = 0,
    ) -> bool:
        """Zapis zmian grafu w jednej transakcji: wiersze pojęć i połączeń, usunięcia, licznik zapomnianych.
        Zwraca True (błąd bazy to wyjątek), żeby wywołujący przez strażnika błędów odróżnił sukces."""
        with self._lock, self._conn:
            _apply(self._conn, concepts, links, delete=delete, delete_links=delete_links, forgotten=forgotten)
        return True

    def delete_concepts(self, keys: Iterable[str]) -> list[Concept]:
        """Usuwa pojęcia (z połączeniami i domenami); zwraca usunięte."""
        keys = list(dict.fromkeys(keys))
        with self._lock, self._conn:
            found = _load_concepts(self._conn, keys)
            self._conn.executemany("DELETE FROM concepts WHERE key = ?", [(k,) for k in found])
        return [found[k] for k in keys if k in found]

    def clear_memory(self) -> int:
        """Usuwa całą pamięć: pojęcia, połączenia, licznik zapomnianych i dawne fakty (`facts_v3`)."""
        with self._lock, self._conn:
            count = self._conn.execute("DELETE FROM concepts").rowcount
            self._conn.execute("DELETE FROM concept_links")
            self._conn.execute("DELETE FROM memory_meta")
            self._conn.execute("DROP TABLE IF EXISTS facts_v3")
        return count

    def concept_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0]

    def forgotten_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT v FROM memory_meta WHERE k = 'forgotten'").fetchone()
        return int(row[0]) if row else 0

    def has_facts_v3(self) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'facts_v3'"
            ).fetchone() is not None


def _to_link(r: sqlite3.Row) -> Link:
    return Link(r["a"], r["b"], r["strength"], _from_db(r["last_used"]))


def _load_concepts(conn: sqlite3.Connection, keys: list[str] | None) -> dict[str, Concept]:
    if keys is not None and not keys:
        return {}
    where, args = "", []
    if keys is not None:
        where, args = f" WHERE key IN ({','.join('?' * len(keys))})", list(keys)
    rows = conn.execute(f"SELECT * FROM concepts{where}", args).fetchall()
    domains: dict[str, dict[str, int]] = {}
    for d in conn.execute(f"SELECT key, domain, count FROM concept_domains{where}", args):
        domains.setdefault(d["key"], {})[d["domain"]] = d["count"]
    return {
        r["key"]: Concept(r["key"], r["label"], r["strength"], _from_db(r["last_used"]), r["hits"],
                          _from_db(r["created_at"]), domains.get(r["key"], {}))
        for r in rows
    }


def _load_links_between(conn: sqlite3.Connection, keys: list[str]) -> dict[tuple[str, str], Link]:
    keys = list(dict.fromkeys(keys))
    if len(keys) < 2:
        return {}
    marks = ",".join("?" * len(keys))
    rows = conn.execute(f"SELECT * FROM concept_links WHERE a IN ({marks}) AND b IN ({marks})", keys + keys)
    return {(r["a"], r["b"]): _to_link(r) for r in rows}


def _apply(
    conn: sqlite3.Connection,
    concepts: Iterable[Concept],
    links: Iterable[Link],
    *,
    delete: Iterable[str] = (),
    delete_links: Iterable[tuple[str, str]] = (),
    forgotten: int = 0,
) -> None:
    for c in concepts:
        conn.execute(
            "INSERT INTO concepts(key, label, strength, last_used, hits, created_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET strength = excluded.strength, last_used = excluded.last_used,"
            " hits = excluded.hits",
            (c.key, c.label, min(1.0, max(0.0, c.strength)), _to_db(c.last_used), c.hits, _to_db(c.created_at)),
        )
        for domain, count in c.domains.items():
            conn.execute(
                "INSERT INTO concept_domains(key, domain, count) VALUES (?, ?, ?)"
                " ON CONFLICT(key, domain) DO UPDATE SET count = excluded.count",
                (c.key, domain, count),
            )
    for link in links:
        conn.execute(
            "INSERT INTO concept_links(a, b, strength, last_used) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(a, b) DO UPDATE SET strength = excluded.strength, last_used = excluded.last_used",
            (link.a, link.b, min(1.0, max(0.0, link.strength)), _to_db(link.last_used)),
        )
    conn.executemany("DELETE FROM concept_links WHERE a = ? AND b = ?", list(delete_links))
    conn.executemany("DELETE FROM concepts WHERE key = ?", [(k,) for k in delete])
    if forgotten:
        conn.execute(
            "INSERT INTO memory_meta(k, v) VALUES ('forgotten', ?)"
            " ON CONFLICT(k) DO UPDATE SET v = CAST(CAST(v AS INTEGER) + ? AS TEXT)",
            (str(forgotten), forgotten),
        )


def _rekey(cfg: MemoryConfig) -> Callable[[sqlite3.Connection], None]:
    """Migracja ze schematu 4: klucze pojęć liczone od nowa z etykiet nową regułą `stem` (spec, sekcja 9)."""

    def run(conn: sqlite3.Connection) -> None:
        from host.core.memory import rekey

        old_links = [_to_link(r) for r in conn.execute("SELECT * FROM concept_links").fetchall()]
        concepts, links = rekey(_load_concepts(conn, None).values(), old_links, cfg)
        conn.execute("DELETE FROM concept_links")
        conn.execute("DELETE FROM concepts")  # liczniki domen przez ON DELETE CASCADE
        _apply(conn, concepts, links)

    return run


def _migrate_facts(cfg: MemoryConfig) -> Callable[[sqlite3.Connection], None]:
    """Migracja z wersji 3: każdy fakt uczy graf heurystyką (bez filtra danych wrażliwych, z filtrem
    liczb), w kolejności i w chwili `updated_at` (spec, sekcja 9)."""

    def run(conn: sqlite3.Connection) -> None:
        from host.core.memory import heuristic, keep, plan_learn

        rows = conn.execute("SELECT text, updated_at FROM facts_v3 ORDER BY updated_at, id").fetchall()
        for r in rows:
            ext = keep(heuristic(r[0], cfg), cfg, allow_sensitive=True)
            keys = [k for k, _ in ext.concepts]
            if not keys:
                continue
            plan = plan_learn(ext, _load_concepts(conn, keys), _load_links_between(conn, keys),
                              _from_db(r[1]), cfg)
            _apply(conn, plan.concepts, plan.links)

    return run
