import json
import logging
from datetime import datetime, timedelta, timezone

from host.core.clock import ManualClock
from host.core.continuity import Continuity
from host.core.core import HostCore
from host.core.self_model import mode_for
from host.tests.helpers import heartbeat, make_event

START = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def frame(type_, body, t="event"):
    return json.dumps({"v": 1, "t": t, "id": "1", "ts": 0, "type": type_, "body": body})


def make(prev_event=None):
    clock = ManualClock(START)
    cont = Continuity.at_start(START, prev_event, prior_sessions=2, known_facts=3)
    return HostCore(clock=clock, continuity=cont), clock


def beats(core, clock, seconds, batt):
    """Heartbeat co 1 s; zwraca (typ, drive) zdarzeń drive.*."""
    out = []
    for _ in range(seconds):
        clock.advance(1)
        for e in core.handle_event(heartbeat(batt)):
            out.append((e.type, e.data.get("drive")))
    return out


def test_mode_rules():
    assert mode_for([]) == "nominal"
    assert mode_for(["temp_high", "long_silence"]) == "nominal"
    assert mode_for(["battery_critical"]) == "saving"
    assert mode_for(["battery_low", "link_lost"]) == "offline"


def test_start_event_carries_continuity():
    core, _ = make(prev_event=START - timedelta(minutes=10))
    (ev,) = core.start()
    assert ev.type == "host.start"
    assert ev.data == {"sessions": 3, "known_facts": 3, "last_downtime_s": 600}


def test_self_snapshot_has_spec_fields():
    core, clock = make(prev_event=START - timedelta(hours=1))
    clock.advance(42)
    snap = core.self_snapshot()
    assert set(snap["self"]) == {
        "name", "version", "host", "mode", "uptime_s", "last_restart",
        "last_downtime_s", "drives", "continuity",
    }
    assert snap["self"]["name"] == "IRIS"
    assert snap["self"]["mode"] == "nominal"
    assert snap["self"]["uptime_s"] == 42
    assert snap["self"]["last_restart"] == START.isoformat()
    assert snap["self"]["last_downtime_s"] == 3600
    assert snap["self"]["continuity"] == {"sessions": 3, "known_facts": 3}
    assert set(snap["body"]) == {
        "batt_pct", "link", "rssi", "temp_c", "sensors_ok", "sensors_down", "last_seen_ms",
    }


def test_low_battery_scenario_switches_mode_and_reports_in_slice():
    core, clock = make()
    assert beats(core, clock, 10, batt=18) == []  # licznik startuje przy pierwszym odczycie
    assert beats(core, clock, 1, batt=18) == [("drive.enter", "battery_low")]
    assert core.self_snapshot()["self"]["mode"] == "saving"
    s = core.prompt_slice()
    assert s["mode"] == "saving"
    assert s["drives"] == ["battery_low"]
    assert s["body_changes"]["batt_pct"] == {"from": None, "to": 18}


def test_link_loss_and_recovery_events():
    core, clock = make()
    core.handle_event(heartbeat(80))
    clock.advance(6)
    ev = core.tick()
    assert [(e.type, e.data.get("drive")) for e in ev] == [
        ("link.lost", None), ("drive.enter", "link_lost"),
    ]
    assert core.self_snapshot()["self"]["mode"] == "offline"
    ev = core.handle_event(heartbeat(80))
    assert [e.type for e in ev] == ["link.restored"]
    assert beats(core, clock, 5, batt=80) == [("drive.exit", "link_lost")]
    assert core.self_snapshot()["self"]["mode"] == "nominal"


def test_late_heartbeat_reports_loss_before_restore_even_without_tick():
    core, clock = make()
    core.handle_event(heartbeat(80))
    clock.advance(30)  # nikt nie wołał tick()
    ev = core.handle_event(heartbeat(80))
    assert [(e.type, e.data.get("drive")) for e in ev] == [
        ("link.lost", None), ("drive.enter", "link_lost"), ("link.restored", None),
    ]


def test_rejected_frames_are_logged_and_ignored(caplog):
    core, _ = make()
    with caplog.at_level(logging.WARNING):
        assert core.handle_frame("{not json") == []
        assert core.handle_frame(frame("teleport", {})) == []
        assert core.handle_frame(frame("body.heartbeat", {"batt_pct": 50}, t="event")) == []
    assert len(caplog.records) == 3
    assert core.body.link == "unknown"


def test_valid_frame_goes_through_handle_frame():
    core, _ = make()
    core.handle_frame(frame("body.heartbeat", {"batt_pct": 64, "link_rssi": -60}))
    assert core.body.snapshot()["batt_pct"] == 64


def test_ack_frame_does_not_change_state():
    core, _ = make()
    assert core.handle_frame(frame("ping", {"ref": "a1", "ok": True}, t="ack")) == []
    assert core.body.link == "unknown"


def test_interaction_events_reset_last_interaction():
    core, clock = make()
    for type_, body in [
        ("audio.utterance.start", {"utt_id": 1}),
        ("local.command", {"cmd": "time", "result": "ok"}),
        ("button", {"id": "b", "kind": "press"}),
    ]:
        clock.advance(10)
        core.handle_event(make_event(type_, body))
        assert core.last_interaction == clock.wall()
    clock.advance(10)
    core.handle_event(make_event("audio.utterance.end", {"utt_id": 1, "dur_ms": 900}))
    assert core.last_interaction != clock.wall()


def test_long_silence_ends_on_button_press():
    core, clock = make()
    clock.advance(4 * 3600 + 1)
    entered = [e.data["drive"] for e in core.tick() if e.type == "drive.enter"]
    assert entered == ["link_lost", "long_silence"]  # ciało nigdy się nie odezwało
    ev = core.handle_event(make_event("button", {"id": "b", "kind": "press"}))
    assert [(e.type, e.data["drive"]) for e in ev] == [("drive.exit", "long_silence")]


def test_slice_reports_body_changes_beyond_tolerance_only():
    core, _ = make()
    core.handle_event(heartbeat(71, -58))
    first = core.prompt_slice()["body_changes"]
    assert first["batt_pct"]["to"] == 71
    assert first["link"] == {"from": None, "to": "ok"}
    assert "temp_c" not in first  # None -> None nie jest zmianą

    core.handle_event(heartbeat(68, -60))
    assert "body_changes" not in core.prompt_slice()  # w granicach tolerancji

    core.handle_event(heartbeat(65, -60))  # 6 pkt od ostatnio raportowanych 71
    assert core.prompt_slice()["body_changes"] == {"batt_pct": {"from": 71, "to": 65}}


def test_slice_reports_sensor_availability_changes():
    core, _ = make()
    core.prompt_slice()
    core.handle_event(make_event("body.fault", {"sensor": "gps", "code": "no_fix"}))
    ch = core.prompt_slice()["body_changes"]
    assert ch["sensors_down"] == {"from": [], "to": ["gps"]}
    assert "gps" not in ch["sensors_ok"]["to"]


def test_slice_includes_downtime_once_and_only_if_long_enough():
    core, _ = make(prev_event=START - timedelta(hours=3))
    assert core.prompt_slice()["last_downtime_s"] == 3 * 3600
    assert "last_downtime_s" not in core.prompt_slice()

    short, _ = make(prev_event=START - timedelta(seconds=30))
    assert "last_downtime_s" not in short.prompt_slice()


def test_slice_has_no_sensor_payloads():
    core, _ = make()
    core.handle_event(make_event("body.sensor", {"gps": {"lat": 52.1, "lon": 21.0}, "presence": True, "imu": [0, 1, 2]}))
    dumped = json.dumps(core.prompt_slice())
    for leaked in ("52.1", "21.0", '"lat"', "[0, 1, 2]", "true"):
        assert leaked not in dumped


def test_state_layers_do_not_import_brain():
    """Rdzeń, store i API muszą działać bez host.brain (sprawdzane w czystym procesie)."""
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys\n"
        "import host.core.core, host.store, host.api.main\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('host.brain'))\n"
        "assert not loaded, loaded\n"
    )
    root = Path(__file__).parents[2]
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_initiative_gate_is_exposed():
    core, _ = make()
    assert core.initiative.acquire() and core.initiative.acquire()
    assert not core.initiative.acquire()


def test_known_facts_are_updated():
    core, _ = make()
    core.set_known_facts(9)
    assert core.self_snapshot()["self"]["continuity"]["known_facts"] == 9
