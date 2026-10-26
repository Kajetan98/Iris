import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from host.core.clock import ManualClock
from host.store import SchemaError, Store, StoreError
from host.store.schema import SCHEMA_VERSION
from host.tests.helpers import heartbeat, make_event

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


def host_event(store, type_="drive.enter", at=T0, **data):
    from host.core.events import HostEvent

    return store.append_host_event(HostEvent(type=type_, at=at, data=data))


# --- schemat ---


def test_schema_version_is_set(tmp_path):
    path = tmp_path / "iris.db"
    Store(path).close()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "events", "state_snapshots", "facts"} <= tables


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "iris.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaError):
        Store(path)


def test_reopen_keeps_data(tmp_path, clock):
    path = tmp_path / "iris.db"
    s = Store(path, clock)
    s.begin_session()
    s.add_fact("Mieszkam w Gdańsku", "stated")
    s.close()
    s2 = Store(path, clock)
    assert [f.text for f in s2.facts()] == ["Mieszkam w Gdańsku"]
    s2.close()


# --- sesje i ciągłość ---


def test_first_session_has_no_downtime(clock):
    s = Store(clock=clock)
    c = s.begin_session()
    assert (c.sessions, c.known_facts, c.last_downtime_s) == (1, 0, 0)
    assert c.last_restart == T0
    assert s.session_id == 1


def test_second_session_downtime_from_last_event(tmp_path, clock):
    path = tmp_path / "iris.db"
    s = Store(path, clock)
    s.begin_session()
    clock.advance(100)
    host_event(s, "host.stop", at=clock.wall())
    s.close()

    clock.advance(3 * 3600)
    s2 = Store(path, clock)
    c = s2.begin_session()
    assert c.sessions == 2
    assert c.last_downtime_s == 3 * 3600
    s2.close()


def test_downtime_without_events_uses_touch_then_start(tmp_path, clock):
    path = tmp_path / "iris.db"
    s = Store(path, clock)
    s.begin_session()
    clock.advance(60)
    s.touch()
    s.close()
    clock.advance(600)
    s2 = Store(path, clock)
    assert s2.begin_session().last_downtime_s == 600
    s2.close()

    # sesja bez żadnej aktywności: odniesieniem jest jej start
    s3 = Store(path, clock)
    clock.advance(50)
    assert s3.begin_session().last_downtime_s == 50
    s3.close()


def test_last_seen_never_moves_backwards(store, clock):
    clock.advance(100)
    store.touch()
    host_event(store, at=T0)  # zdarzenie z przeszłości
    assert store.sessions()[0].last_seen_at == T0 + timedelta(seconds=100)


def test_known_facts_and_prior_sessions_feed_continuity(tmp_path, clock):
    path = tmp_path / "iris.db"
    s = Store(path, clock)
    s.begin_session()
    s.add_fact("a", "stated")
    s.add_fact("b", "inferred", 0.4)
    s.close()
    s2 = Store(path, clock)
    c = s2.begin_session()
    assert (c.sessions, c.known_facts) == (2, 2)
    s2.close()


def test_end_session_sets_ended_at(store, clock):
    clock.advance(30)
    store.end_session()
    row = store.sessions()[0]
    assert row.ended_at == T0 + timedelta(seconds=30)
    assert row.last_seen_at == row.ended_at


def test_session_required_for_writes(clock):
    s = Store(clock=clock)
    with pytest.raises(StoreError):
        host_event(s)
    with pytest.raises(StoreError):
        s.add_fact("x", "stated")
    s.begin_session()
    with pytest.raises(StoreError):
        s.begin_session()


# --- zdarzenia ---


def test_event_roundtrip_with_polish_text(store):
    host_event(store, "drive.enter", drive="battery_low", value=18.5, note="zażółć gęślą jaźń")
    (ev,) = store.events()
    assert ev.type == "drive.enter" and ev.source == "host"
    assert ev.data == {"drive": "battery_low", "value": 18.5, "note": "zażółć gęślą jaźń"}
    assert ev.at == T0 and ev.session_id == 1


def test_body_event_keeps_body_ts_and_boot_id(store, clock):
    decoded = make_event("body.heartbeat", {"batt_pct": 71, "link_rssi": -58}, ts=12345)
    clock.advance(5)
    store.append_body_event(decoded, boot_id="b7")
    (ev,) = store.events()
    assert (ev.source, ev.type, ev.ts_ms, ev.boot_id) == ("body", "body.heartbeat", 12345, "b7")
    assert ev.data["batt_pct"] == 71
    assert ev.at == T0 + timedelta(seconds=5)


def test_events_filter_by_type_since_and_paging(store):
    for i in range(5):
        host_event(store, "drive.enter" if i % 2 == 0 else "drive.exit", at=T0 + timedelta(minutes=i), n=i)
    assert [e.data["n"] for e in store.events(types=["drive.exit"])] == [1, 3]
    assert [e.data["n"] for e in store.events(since=T0 + timedelta(minutes=3))] == [3, 4]
    assert [e.data["n"] for e in store.events(types=["drive.enter", "drive.exit"], limit=2)] == [0, 1]
    first_page = store.events(limit=2)
    assert [e.data["n"] for e in store.events(after_id=first_page[-1].id, limit=2)] == [2, 3]
    assert store.events(types=[]) == []


def test_times_are_normalized_to_utc(store):
    tz = timezone(timedelta(hours=2))
    host_event(store, at=datetime(2026, 3, 1, 13, 0, tzinfo=tz), n=1)  # 11:00 UTC, przed T0
    host_event(store, at=datetime(2026, 3, 1, 14, 0, tzinfo=tz), n=2)  # 12:00 UTC = T0
    host_event(store, at=datetime(2026, 3, 1, 11, 59, tzinfo=timezone.utc), n=3)
    assert [e.data["n"] for e in store.events(since=T0)] == [2]
    assert store.events()[1].at == T0


def test_naive_datetime_is_rejected(store):
    with pytest.raises(ValueError, match="strefy"):
        host_event(store, at=datetime(2026, 3, 1, 12, 0))
    with pytest.raises(ValueError):
        store.events(since=datetime(2026, 3, 1))


def test_unknown_source_is_rejected_by_schema(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.append_event(at=T0, source="cloud", type="x", data={})


def test_failed_insert_leaves_no_partial_state(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.append_event(at=T0, source="cloud", type="x", data={})
    assert store.events() == []
    host_event(store)  # połączenie nadal działa
    assert len(store.events()) == 1


def test_concurrent_appends(store):
    def work(k):
        for i in range(50):
            host_event(store, "drive.enter", k=k, i=i)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.events(limit=1000)) == 200


# --- migawki ---


def test_snapshots(store, clock):
    assert store.latest_snapshot() is None
    store.save_snapshot({"mode": "nominal"}, {"batt_pct": 71})
    clock.advance(60)
    store.save_snapshot({"mode": "saving"}, {"batt_pct": 18, "sensors_down": ["gps"]})
    snaps = store.snapshots()
    assert [s.self_state["mode"] for s in snaps] == ["nominal", "saving"]
    assert store.snapshots(since=T0 + timedelta(seconds=30))[0].body["batt_pct"] == 18
    assert store.latest_snapshot().body["sensors_down"] == ["gps"]


# --- fakty ---


def test_stated_fact_defaults_to_full_confidence(store):
    f = store.add_fact("Mam psa o imieniu Burek", "stated")
    assert (f.source, f.confidence, f.session_id) == ("stated", 1.0, 1)
    assert f.created_at == f.updated_at == T0


def test_inferred_fact_requires_confidence(store):
    with pytest.raises(ValueError, match="confidence"):
        store.add_fact("Lubi kawę", "inferred")
    assert store.add_fact("Lubi kawę", "inferred", 0.6).confidence == 0.6


@pytest.mark.parametrize("bad", [-0.1, 1.1, True])
def test_confidence_range(store, bad):
    with pytest.raises(ValueError):
        store.add_fact("x", "inferred", bad)


def test_rejects_bad_source_and_empty_text(store):
    with pytest.raises(ValueError):
        store.add_fact("x", "guessed", 0.5)
    with pytest.raises(ValueError):
        store.add_fact("   ", "stated")
    assert store.facts() == []


def test_list_and_filter_facts(store):
    store.add_fact("a", "stated")
    store.add_fact("b", "inferred", 0.3)
    assert [f.text for f in store.facts()] == ["a", "b"]
    assert [f.text for f in store.facts(source="inferred")] == ["b"]
    assert store.count_facts() == 2


def test_correcting_text_makes_fact_stated(store, clock):
    f = store.add_fact("Pracuje w Krakowie", "inferred", 0.5)
    clock.advance(10)
    g = store.update_fact(f.id, text="Pracuje w Warszawie")
    assert (g.text, g.source, g.confidence) == ("Pracuje w Warszawie", "stated", 1.0)
    assert g.created_at == T0 and g.updated_at == T0 + timedelta(seconds=10)


def test_changing_only_confidence_keeps_source(store):
    f = store.add_fact("Lubi jazz", "inferred", 0.5)
    g = store.update_fact(f.id, confidence=0.9)
    assert (g.text, g.source, g.confidence) == ("Lubi jazz", "inferred", 0.9)


def test_explicit_source_and_confidence_win_over_text_rule(store):
    f = store.add_fact("a", "stated")
    g = store.update_fact(f.id, text="b", source="inferred", confidence=0.4)
    assert (g.text, g.source, g.confidence) == ("b", "inferred", 0.4)


def test_same_text_does_not_promote_inferred(store):
    f = store.add_fact("a", "inferred", 0.5)
    g = store.update_fact(f.id, text="a")
    assert (g.source, g.confidence) == ("inferred", 0.5)


def test_update_invalid_values(store):
    f = store.add_fact("a", "stated")
    for kwargs in ({"text": " "}, {"confidence": 2}, {"source": "x"}):
        with pytest.raises(ValueError):
            store.update_fact(f.id, **kwargs)
    assert store.get_fact(f.id).text == "a"


def test_update_missing_fact_returns_none(store):
    assert store.update_fact(99, text="x") is None


def test_delete_fact_is_permanent(store):
    f = store.add_fact("a", "stated")
    assert store.delete_fact(f.id) is True
    assert store.delete_fact(f.id) is False
    assert store.get_fact(f.id) is None
    assert store.count_facts() == 0


def test_ping(store):
    store.ping()
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        store.ping()
