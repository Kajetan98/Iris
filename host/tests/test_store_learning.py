"""Baza: schemat v3 i operacje uczenia się (docs/spec.md, sekcja 9)."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from host.core.clock import ManualClock
from host.store import Store
from host.store.schema import SCHEMA_VERSION, _V1, _V2

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock():
    return ManualClock(T0)


@pytest.fixture
def store(clock):
    s = Store(clock=clock)
    s.begin_session()
    yield s
    s.close()


def test_migration_from_v2_keeps_facts(tmp_path, clock):
    path = tmp_path / "iris.db"
    conn = sqlite3.connect(path)
    conn.executescript(f"BEGIN;{_V1}{_V2}PRAGMA user_version = 2;COMMIT;")
    conn.execute("INSERT INTO facts(text, source, confidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                 ("Lubi kawę", "inferred", 0.7, "2026-02-01T10:00:00.000+00:00", "2026-02-01T10:00:00.000+00:00"))
    conn.commit()
    conn.close()
    s = Store(path, clock)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    (f,) = s.facts()
    assert (f.text, f.confidence, f.reinforced, f.asked_at) == ("Lubi kawę", 0.7, 0, None)
    s.close()


def test_reinforce_grows_up_to_the_cap(store, clock):
    f = store.add_fact("Lubisz kawę", "inferred", 0.45)
    clock.advance(60)
    f = store.reinforce_fact(f.id, 0.2, 0.9)
    assert (f.confidence, f.reinforced, f.updated_at) == (pytest.approx(0.65), 1, T0 + timedelta(seconds=60))
    for _ in range(3):
        f = store.reinforce_fact(f.id, 0.2, 0.9)
    assert (f.confidence, f.reinforced) == (0.9, 4)
    high = store.add_fact("Masz psa", "inferred", 0.95)
    assert store.reinforce_fact(high.id, 0.2, 0.9).confidence == 0.95  # wzmocnienie nie obniża
    assert store.reinforce_fact(999, 0.2, 0.9) is None


def test_asking_does_not_stop_decay(store, clock):
    f = store.add_fact("Lubisz kawę", "inferred", 0.5)
    clock.advance(3600)
    f = store.mark_fact_asked(f.id)
    assert f.asked_at == T0 + timedelta(hours=1) and f.updated_at == T0


def test_decay_weakens_then_forgets_only_guesses(store, clock):
    weak = store.add_fact("Grasz w szachy", "inferred", 0.25)
    mid = store.add_fact("Lubisz kawę", "inferred", 0.6)
    told = store.add_fact("Mam psa", "stated")
    clock.advance(31 * 86400)
    fresh = store.add_fact("Jeździsz rowerem", "inferred", 0.4)
    weakened, forgotten = store.decay_facts(clock.wall() - timedelta(days=30), 0.1, 0.2)
    assert (weakened, forgotten) == ([mid.id], [weak.id])
    by_id = {f.id: f for f in store.facts()}
    assert by_id[mid.id].confidence == pytest.approx(0.5) and by_id[mid.id].updated_at == clock.wall()
    assert by_id[told.id].confidence == 1.0 and by_id[fresh.id].confidence == 0.4
    # odświeżony updated_at: drugie zanikanie dopiero po kolejnych 30 dniach
    assert store.decay_facts(clock.wall() - timedelta(days=30), 0.1, 0.2) == ([], [])


def test_delete_all(store):
    for t in ("a b c", "d e f"):
        store.add_fact(t, "stated")
    assert store.delete_all_facts() == 2 and store.count_facts() == 0
