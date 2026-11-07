import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from host.core.clock import ManualClock
from host.store import SchemaError, Store, StoreError
from host.store.models import Concept, Link
from host.store.schema import SCHEMA_VERSION
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
    assert {"sessions", "events", "state_snapshots", "concepts", "concept_domains", "concept_links",
            "memory_meta", "facts_v3"} <= tables
    assert "facts" not in tables


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
    s.save_memory([concept("lamp", "lampa")])
    s.close()
    s2 = Store(path, clock)
    assert [c.label for c in s2.concepts().values()] == ["lampa"]
    s2.close()


# --- sesje i ciągłość ---


def test_first_session_has_no_downtime(clock):
    s = Store(clock=clock)
    c = s.begin_session()
    assert (c.sessions, c.known_concepts, c.last_downtime_s) == (1, 0, 0)
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


def test_known_concepts_and_prior_sessions_feed_continuity(tmp_path, clock):
    path = tmp_path / "iris.db"
    s = Store(path, clock)
    s.begin_session()
    s.save_memory([concept("lamp"), concept("salon")])
    s.close()
    s2 = Store(path, clock)
    c = s2.begin_session()
    assert (c.sessions, c.known_concepts) == (2, 2)
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


# --- pamięć: graf pojęć ---


def concept(key, label=None, strength=0.35, at=T0, hits=1, domains=None):
    return Concept(key, label or key, strength, at, hits, at, domains or {"rozmowa": 1})


def test_concepts_round_trip_with_domains_and_keep_the_first_label(store):
    store.save_memory([concept("lamp", "lampa", domains={"dom": 2, "czas": 1})])
    store.save_memory([concept("lamp", "lampę", 0.8, T0 + timedelta(days=1), 3, {"dom": 3, "czas": 1})])
    got = store.concepts(["lamp", "brak"])["lamp"]
    assert (got.label, got.strength, got.hits, got.domains) == ("lampa", 0.8, 3, {"dom": 3, "czas": 1})
    assert got.last_used == T0 + timedelta(days=1) and got.created_at == T0
    assert store.concepts([]) == {} and store.concept_count() == 1


def test_links_between_touching_and_cascade_on_delete(store):
    store.save_memory([concept("lamp"), concept("salon"), concept("kuchni")],
                      [Link("lamp", "salon", 0.3, T0), Link("kuchni", "lamp", 0.5, T0)])
    assert set(store.links_between(["lamp", "salon"])) == {("lamp", "salon")}
    assert {(l.a, l.b) for l in store.links_touching(["salon"])} == {("lamp", "salon")}
    assert len(store.all_links()) == 2 and store.links_touching([]) == []
    removed = store.delete_concepts(["lamp", "brak"])
    assert [c.key for c in removed] == ["lamp"]
    assert store.all_links() == [] and store.concept_count() == 2


def test_link_order_and_strength_range_are_enforced(store):
    store.save_memory([concept("aaaa"), concept("bbbb")])
    with pytest.raises(sqlite3.IntegrityError):
        store.save_memory(links=[Link("bbbb", "aaaa", 0.3, T0)])
    store.save_memory([concept("aaaa", strength=1.0000001)])  # błąd zaokrąglenia jest przycinany do 1
    assert store.concepts(["aaaa"])["aaaa"].strength == 1.0


def test_deletions_and_forgotten_counter_in_one_transaction(store):
    store.save_memory([concept("lamp"), concept("salon")], [Link("lamp", "salon", 0.3, T0)])
    store.save_memory(delete=["lamp"], delete_links=[("lamp", "salon")], forgotten=1)
    store.save_memory(forgotten=2)
    assert list(store.concepts()) == ["salon"] and store.forgotten_count() == 3


def test_clear_memory_removes_everything_and_the_old_facts_table(store):
    store.save_memory([concept("lamp")], forgotten=4)
    assert store.has_facts_v3()
    assert store.clear_memory() == 1
    assert store.concept_count() == 0 and store.forgotten_count() == 0 and not store.has_facts_v3()
    assert store.clear_memory() == 0  # drugi raz bez błędu
