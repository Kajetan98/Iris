"""Urządzenia w bazie: schemat v2, migracja z v1, aktywne urządzenie (docs/spec.md, sekcja 9)."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from host.core.clock import ManualClock
from host.core.events import HostEvent
from host.store import Store
from host.store.schema import SCHEMA_VERSION, _V1
from host.tests.helpers import make_event

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


def v1_database(path, *, body_events=True):
    """Baza w wersji 1 (jedno ciało), zapisana tak, jak zapisywał ją host przed zmianą."""
    conn = sqlite3.connect(path)
    conn.executescript(f"BEGIN;{_V1}PRAGMA user_version = 1;COMMIT;")
    conn.execute("INSERT INTO sessions(started_at, last_seen_at) VALUES (?, ?)",
                 ("2026-02-01T10:00:00.000+00:00", "2026-02-01T11:00:00.000+00:00"))
    rows = [("host", "host.start", "2026-02-01T10:00:00.000+00:00"),
            ("host", "link.lost", "2026-02-01T10:00:05.000+00:00"),
            ("host", "drive.enter", "2026-02-01T10:00:05.000+00:00"),
            ("host", "action.sent", "2026-02-01T10:20:00.000+00:00")]
    if body_events:
        rows += [("body", "system.boot", "2026-02-01T10:01:00.000+00:00"),
                 ("body", "button", "2026-02-01T10:59:00.000+00:00")]
    for source, type_, at in rows:
        conn.execute("INSERT INTO events(session_id, at, source, type, data) VALUES (1, ?, ?, ?, '{}')",
                     (at, source, type_))
    conn.commit()
    conn.close()


def test_migration_from_single_body_database(tmp_path, clock):
    path = tmp_path / "iris.db"
    v1_database(path)
    s = Store(path, clock)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    by_type = {e.type: e.device for e in s.events()}
    assert by_type == {
        "host.start": None, "drive.enter": None,
        "link.lost": "hermes", "action.sent": "hermes", "system.boot": "hermes", "button": "hermes",
    }
    (dev,) = s.devices()
    assert dev.id == "hermes" and dev.active
    assert dev.first_seen_at == datetime(2026, 2, 1, 10, 1, tzinfo=timezone.utc)
    assert dev.last_seen_at == datetime(2026, 2, 1, 10, 59, tzinfo=timezone.utc)
    assert s.active_device() == "hermes"
    s.close()
    Store(path, clock).close()  # ponowne otwarcie nie migruje drugi raz


def test_migration_without_body_events_has_no_devices(tmp_path, clock):
    path = tmp_path / "iris.db"
    v1_database(path, body_events=False)
    with Store(path, clock) as s:
        assert s.devices() == [] and s.active_device() is None


def test_device_registry_and_active_device_survive_restart(tmp_path, clock):
    path = tmp_path / "iris.db"
    s = Store(path, clock)
    s.begin_session()
    s.see_device("hermes")
    clock.advance(30)
    s.see_device("laptop")
    clock.advance(30)
    s.see_device("hermes")
    s.touch_devices(["laptop"])
    assert [(d.id, d.last_seen_at - T0, d.active) for d in s.devices()] == [
        ("hermes", timedelta(seconds=60), False),
        ("laptop", timedelta(seconds=60), False),
    ]
    assert s.devices()[1].first_seen_at == T0 + timedelta(seconds=30)
    assert s.active_device() is None
    s.set_active_device("laptop")
    s.set_active_device("hermes")
    s.set_active_device("hermes")
    assert [d.active for d in s.devices()] == [True, False]
    with pytest.raises(KeyError):
        s.set_active_device("phone")
    assert s.device_known("laptop") and not s.device_known("phone")
    s.close()
    with Store(path, clock) as again:
        assert again.active_device() == "hermes"


def test_last_seen_of_device_never_moves_backwards(store, clock):
    store.see_device("hermes")
    clock.advance(-60)
    store.see_device("hermes")
    store.touch_devices(["hermes"])
    assert store.devices()[0].last_seen_at == T0


def test_schema_allows_one_active_device(store):
    store.see_device("hermes")
    store.see_device("laptop")
    store.set_active_device("hermes")
    with pytest.raises(sqlite3.IntegrityError):
        with store._conn:  # noqa: SLF001  sprawdzamy ograniczenie w samej bazie
            store._conn.execute("UPDATE devices SET active = 1 WHERE id = 'laptop'")  # noqa: SLF001


def test_events_carry_and_filter_by_device(store):
    store.append_body_event(make_event("button", {"id": "b1", "kind": "press"}), device="laptop")
    store.append_body_event(make_event("button", {"id": "b1", "kind": "press"}), device="hermes")
    store.append_host_event(HostEvent(type="link.lost", at=T0, device="laptop"))
    store.append_host_event(HostEvent(type="host.stop", at=T0))
    assert [(e.type, e.device) for e in store.events()] == [
        ("button", "laptop"), ("button", "hermes"), ("link.lost", "laptop"), ("host.stop", None),
    ]
    assert [e.type for e in store.events(device="laptop")] == ["button", "link.lost"]
    assert [e.type for e in store.events(device="laptop", types=["link.lost"])] == ["link.lost"]
