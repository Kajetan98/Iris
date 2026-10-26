"""Store razem z HostCore: ciągłość między restartami."""

import json
from datetime import datetime, timezone

from host.core.clock import ManualClock
from host.core.core import HostCore
from host.core.protocol import decode_frame
from host.store import Store

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def frame(type_, body):
    return json.dumps({"v": 1, "t": "event", "id": "1", "ts": 500, "type": type_, "body": body})


def run_session(path, clock, *, seconds, batt, fact=None):
    """Jedna sesja hosta: heartbeat co 1 s, zapis zdarzeń ciała i hosta, czyste zamknięcie."""
    store = Store(path, clock)
    core = HostCore(clock=clock, continuity=store.begin_session())
    if fact:
        store.add_fact(fact, "stated")
    for ev in core.start():
        store.append_host_event(ev)
    for _ in range(seconds):
        clock.advance(1)
        raw = frame("body.heartbeat", {"batt_pct": batt, "link_rssi": -60})
        store.append_body_event(decode_frame(raw))
        for ev in core.handle_frame(raw):
            store.append_host_event(ev)
    snap = core.self_snapshot()
    store.save_snapshot(snap["self"], snap["body"])
    for ev in core.stop():
        store.append_host_event(ev)
    store.end_session()
    store.close()
    return core, snap


def test_restart_reports_downtime_and_session_count(tmp_path):
    path = tmp_path / "iris.db"
    clock = ManualClock(T0)
    run_session(path, clock, seconds=15, batt=18, fact="Lubię herbatę")
    clock.advance(2 * 3600)

    store = Store(path, clock)
    continuity = store.begin_session()
    core = HostCore(clock=clock, continuity=continuity)

    snap = core.self_snapshot()["self"]
    assert snap["last_downtime_s"] == 2 * 3600
    assert snap["continuity"] == {"sessions": 2, "known_facts": 1}
    assert snap["last_restart"] == clock.wall().isoformat()
    assert core.prompt_slice()["last_downtime_s"] == 2 * 3600
    store.close()


def test_events_from_first_session_are_queryable(tmp_path):
    path = tmp_path / "iris.db"
    clock = ManualClock(T0)
    run_session(path, clock, seconds=15, batt=18)

    store = Store(path, clock)
    types = [e.type for e in store.events(types=["host.start", "drive.enter", "host.stop"])]
    assert types == ["host.start", "drive.enter", "host.stop"]
    drive = store.events(types=["drive.enter"])[0]
    assert drive.data["drive"] == "battery_low"
    assert store.latest_snapshot().self_state["mode"] == "saving"
    heartbeats = store.events(types=["body.heartbeat"])
    assert len(heartbeats) == 15 and heartbeats[0].ts_ms == 500
    store.close()
