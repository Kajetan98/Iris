from host.core.body import BodyState
from host.core.clock import ManualClock
from host.core.config import Config
from host.tests.helpers import heartbeat, make_event


def make():
    clock = ManualClock()
    return BodyState(Config(), clock), clock


def test_initial_snapshot_has_no_data():
    body, _ = make()
    snap = body.snapshot()
    assert snap["link"] == "unknown"
    assert snap["batt_pct"] is None and snap["temp_c"] is None
    assert snap["last_seen_ms"] is None
    assert snap["sensors_down"] == []
    assert snap["sensors_ok"] == ["mic", "imu", "temp", "presence", "gps"]


def test_first_heartbeat_sets_link_ok_without_restored_event():
    body, _ = make()
    assert body.apply(heartbeat(71, -58)) == []
    snap = body.snapshot()
    assert (snap["link"], snap["batt_pct"], snap["rssi"]) == ("ok", 71, -58)


def test_sensor_event_updates_temperature_only_when_present():
    body, _ = make()
    body.apply(make_event("body.sensor", {"temp_c": 24.1}))
    body.apply(make_event("body.sensor", {"presence": True}))
    assert body.snapshot()["temp_c"] == 24.1


def test_link_lost_after_timeout_following_heartbeat():
    body, clock = make()
    body.apply(heartbeat())
    clock.advance(5)
    assert body.tick() == []  # dokładnie 5 s to jeszcze nie "> 5 s"
    clock.advance(0.1)
    events = body.tick()
    assert [e.type for e in events] == ["link.lost"]
    assert body.link == "lost"
    assert body.tick() == []  # tylko raz


def test_link_lost_without_any_heartbeat_counts_from_host_start():
    body, clock = make()
    clock.advance(4)
    assert body.tick() == []
    assert body.link == "unknown"
    clock.advance(1.5)
    assert [e.type for e in body.tick()] == ["link.lost"]


def test_heartbeat_after_loss_emits_link_restored():
    body, clock = make()
    body.apply(heartbeat())
    clock.advance(6)
    body.tick()
    events = body.apply(heartbeat())
    assert [e.type for e in events] == ["link.restored"]
    assert body.link == "ok"


def test_non_heartbeat_frames_do_not_keep_link_alive():
    body, clock = make()
    body.apply(heartbeat())
    for _ in range(6):
        clock.advance(1)
        body.apply(make_event("body.sensor", {"temp_c": 20}))
    assert [e.type for e in body.tick()] == ["link.lost"]


def test_fault_and_clear():
    body, _ = make()
    body.apply(make_event("body.fault", {"sensor": "gps", "code": "no_fix"}))
    snap = body.snapshot()
    assert snap["sensors_down"] == ["gps"]
    assert "gps" not in snap["sensors_ok"]
    body.apply(make_event("body.fault.clear", {"sensor": "gps"}))
    assert body.snapshot()["sensors_down"] == []


def test_clear_of_unknown_fault_is_harmless():
    body, _ = make()
    body.apply(make_event("body.fault.clear", {"sensor": "mic"}))
    assert body.snapshot()["sensors_down"] == []


def test_unlisted_sensor_fault_is_reported_down():
    body, _ = make()
    body.apply(make_event("body.fault", {"sensor": "lidar", "code": 3}))
    snap = body.snapshot()
    assert snap["sensors_down"] == ["lidar"]
    assert "lidar" not in snap["sensors_ok"]


def test_boot_clears_faults_and_stores_boot_info():
    body, _ = make()
    body.apply(make_event("body.fault", {"sensor": "imu", "code": 1}))
    body.apply(make_event("system.boot", {"boot_id": "b2", "fw": "0.3", "reset_reason": "watchdog"}))
    assert body.snapshot()["sensors_down"] == []
    assert (body.boot_id, body.fw, body.reset_reason) == ("b2", "0.3", "watchdog")


def test_last_seen_ms_is_age_of_last_frame():
    body, clock = make()
    body.apply(heartbeat())
    clock.advance(2.5)
    assert body.snapshot()["last_seen_ms"] == 2500


def test_frame_errors_counter_is_kept():
    body, _ = make()
    body.apply(heartbeat(frame_errors=3))
    body.apply(heartbeat())  # brak pola nie zeruje licznika
    assert body.frame_errors == 3
