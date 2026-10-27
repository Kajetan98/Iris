"""Schemat SQLite (docs/spec.md, sekcja 9). Wersja w PRAGMA user_version."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

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

_MIGRATIONS = {0: _V1 + _V2, 1: _V2}


class SchemaError(Exception):
    pass


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SchemaError(
            f"baza ma schemat v{version}, ten program obsługuje do v{SCHEMA_VERSION}"
        )
    script = _MIGRATIONS.get(version)
    if script is None:
        return
    # executescript robi własny COMMIT, więc wersję ustawiamy w tym samym skrypcie.
    try:
        conn.executescript(f"BEGIN;{script}PRAGMA user_version = {SCHEMA_VERSION};COMMIT;")
    except sqlite3.Error:
        conn.rollback()
        raise
