"""Schemat SQLite (docs/spec.md, sekcja 9). Wersja w PRAGMA user_version."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

SCHEMA_VERSION = 5

_V1 = """
CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    at         TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('body', 'host')),
    type       TEXT NOT NULL,
    ts_ms      INTEGER,
    boot_id    TEXT,
    data       TEXT NOT NULL
);
CREATE INDEX events_type_at ON events(type, at);
CREATE INDEX events_at ON events(at);

CREATE TABLE state_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    at         TEXT NOT NULL,
    self_json  TEXT NOT NULL,
    body_json  TEXT NOT NULL
);
CREATE INDEX state_snapshots_at ON state_snapshots(at);

CREATE TABLE facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL CHECK (length(trim(text)) > 0),
    source     TEXT NOT NULL CHECK (source IN ('stated', 'inferred')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    session_id INTEGER REFERENCES sessions(id)
);
"""


# Wersja 2: wiele urządzeń (docs/spec.md, sekcje 1 i 9).
_V2 = """
ALTER TABLE events ADD COLUMN device_id TEXT;
UPDATE events SET device_id = 'hermes'
    WHERE source = 'body' OR type IN ('link.lost', 'link.restored', 'action.sent', 'action.ack');
CREATE INDEX events_device ON events(device_id, id);

CREATE TABLE devices (
    id            TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1))
);
CREATE UNIQUE INDEX devices_one_active ON devices(active) WHERE active = 1;

INSERT INTO devices(id, first_seen_at, last_seen_at, active)
    SELECT 'hermes', MIN(at), MAX(at), 1 FROM events WHERE source = 'body' HAVING COUNT(*) > 0;
"""

# Wersja 3: uczenie się faktów (docs/spec.md, sekcja 9, "Uczenie się").
_V3 = """
ALTER TABLE facts ADD COLUMN reinforced INTEGER NOT NULL DEFAULT 0 CHECK (reinforced >= 0);
ALTER TABLE facts ADD COLUMN asked_at TEXT;
"""

_MIGRATIONS = {0: _V1 + _V2 + _V3, 1: _V2 + _V3, 2: _V3}

# Wersja 4: pamięć jako graf pojęć (docs/spec.md, sekcja 9, "Pamięć: graf pojęć"). Fakty uczą graf
# w tej samej transakcji (wywołanie zwrotne), a tabela `facts` zostaje jako `facts_v3`.
_V4 = [
    """CREATE TABLE concepts (
        key        TEXT PRIMARY KEY CHECK (length(key) > 0),
        label      TEXT NOT NULL,
        strength   REAL NOT NULL CHECK (strength BETWEEN 0 AND 1),
        last_used  TEXT NOT NULL,
        hits       INTEGER NOT NULL CHECK (hits >= 1),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE concept_domains (
        key    TEXT NOT NULL REFERENCES concepts(key) ON DELETE CASCADE,
        domain TEXT NOT NULL,
        count  INTEGER NOT NULL CHECK (count >= 1),
        PRIMARY KEY (key, domain)
    )""",
    """CREATE TABLE concept_links (
        a         TEXT NOT NULL REFERENCES concepts(key) ON DELETE CASCADE,
        b         TEXT NOT NULL REFERENCES concepts(key) ON DELETE CASCADE,
        strength  REAL NOT NULL CHECK (strength BETWEEN 0 AND 1),
        last_used TEXT NOT NULL,
        PRIMARY KEY (a, b),
        CHECK (a < b)
    )""",
    "CREATE INDEX concept_links_b ON concept_links(b)",
    "CREATE TABLE memory_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)",
    "ALTER TABLE facts RENAME TO facts_v3",
]


class SchemaError(Exception):
    pass


def migrate(
    conn: sqlite3.Connection,
    on_v4: Callable[[sqlite3.Connection], None] | None = None,
    on_v5: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
    """Podnosi schemat do SCHEMA_VERSION. `on_v4` dostaje połączenie w transakcji migracji do v4,
    po utworzeniu tabel pamięci i zmianie nazwy `facts` na `facts_v3` (migracja faktów do grafu);
    `on_v5` w transakcji migracji do v5 (przeliczenie kluczy pojęć po zmianie reguły `stem`)."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SchemaError(
            f"baza ma schemat v{version}, ten program obsługuje do v{SCHEMA_VERSION}"
        )
    script = _MIGRATIONS.get(version)
    if script is not None:
        # executescript robi własny COMMIT, więc wersję ustawiamy w tym samym skrypcie.
        try:
            conn.executescript(f"BEGIN;{script}PRAGMA user_version = 3;COMMIT;")
        except sqlite3.Error:
            conn.rollback()
            raise
        version = 3
    if version == 3:
        _step(conn, 4, _V4, on_v4)
        version = 4
    if version == 4:
        _step(conn, 5, [], on_v5)


def _step(conn: sqlite3.Connection, target: int, statements: list[str],
          hook: Callable[[sqlite3.Connection], None] | None) -> None:
    """Jedna migracja w jednej transakcji: instrukcje schematu, funkcja danych i nowa wersja."""
    try:
        conn.execute("BEGIN")
        for statement in statements:
            conn.execute(statement)
        if hook is not None:
            hook(conn)
        conn.execute(f"PRAGMA user_version = {target}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
