"""Wiele urządzeń, jedno aktywne (docs/spec.md, sekcje 1, 7, 8)."""

from datetime import datetime, timezone

import pytest

from host.core.clock import ManualClock
from host.core.core import HostCore
from host.core.protocol import DeviceActivate, decode_frame, encode_action, valid_device_id
from host.tests.helpers import heartbeat, make_event

START = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def make(active=None):
    clock = ManualClock(START)
    return HostCore(clock=clock, active_device=active), clock


def summary(events):
    return [(e.type, e.device, e.data.get("drive") or e.data.get("to")) for e in events]


def beat(core, clock, device, seconds=1, batt=71):
    out = []
    for _ in range(seconds):
        clock.advance(1)
        out += core.handle_event(heartbeat(batt), device)
    return out


def beat_all(core, clock, seconds, **batt):
    """Heartbeat co 1 s od każdego z podanych urządzeń (urządzenie=bateria)."""
    out = []
    for _ in range(seconds):
        clock.advance(1)
        for device, pct in batt.items():
            out += core.handle_event(heartbeat(pct), device)
    return out


def drive_events(events):
    return [e for e in events if e.type.startswith("drive.")]


def test_device_ids():
    for ok in ("hermes", "laptop", "pi-kuchnia", "7", "a" * 32):
        assert valid_device_id(ok), ok
    for bad in ("", "-pi", "Laptop", "pi_kuchnia", "kuchnia ", "a" * 33, "żółw", "pi/1"):
        assert not valid_device_id(bad), bad


def test_device_protocol_messages():
    ev = decode_frame('{"v":1,"t":"event","id":"1","ts":0,"type":"device.activate","body":{}}')
    assert isinstance(ev.body, DeviceActivate)
    frame = encode_action("device.active", {"active": False}, id="h1", ts_ms=0)
    assert '"type":"device.active","body":{"active":false}' in frame
    for bad in ({}, {"active": 1}, {"active": "true"}):
        with pytest.raises(ValueError):
            encode_action("device.active", bad, id="h1", ts_ms=0)


def test_first_device_takes_over_state_from_before_any_device():
    core, clock = make()
    clock.advance(6)
    assert summary(core.tick()) == [("link.lost", None, None), ("drive.enter", None, "link_lost")]
    assert summary(core.connect("laptop")) == [("device.switch", "laptop", "laptop")]
    assert core.active_device == "laptop" and core.self_snapshot()["self"]["device"] == "laptop"
    # jak przy jednym ciele: link.restored i wyjście z link_lost po link_recover_s
    assert summary(beat(core, clock, "laptop")) == [("link.restored", "laptop", None)]
    assert summary(beat(core, clock, "laptop", 5)) == [("drive.exit", "laptop", "link_lost")]
    switch = core.connect("hermes")
    assert switch == [] and core.active_device == "laptop"


def test_inactive_device_is_tracked_but_does_not_drive_state():
    core, clock = make()
    beat(core, clock, "hermes")
    core.connect("laptop")
    before = core.last_interaction
    events = beat_all(core, clock, 15, hermes=71, laptop=5)
    events += core.handle_event(make_event("button", {"id": "b1", "kind": "press"}), "laptop")
    events += core.handle_event(make_event("body.fault", {"sensor": "mic", "code": "x"}), "laptop")
    assert drive_events(events) == []
    assert core.last_interaction == before
    assert core.body.batt_pct == 71 and core.body.sensors_down == []
    laptop = core.device_state("laptop")
    assert laptop["batt_pct"] == 5 and laptop["sensors_down"] == ["mic"] and laptop["link"] == "ok"
    assert core.known_devices() == ["hermes", "laptop"]
    assert core.device_state("phone") is None


def test_link_of_each_device_is_tracked_separately():
    core, clock = make()
    beat(core, clock, "hermes")
    core.connect("laptop")
    beat(core, clock, "laptop")
    for _ in range(6):  # tylko hermes wysyła heartbeat
        clock.advance(1)
        events = core.handle_event(heartbeat(), "hermes")
        if events:
            break
    assert summary(events) == [("link.lost", "laptop", None)]  # bez link_lost: laptop nie jest aktywny
    assert core.drives.active() == []
    assert summary(beat(core, clock, "laptop")) == [("link.restored", "laptop", None)]


def test_switch_ends_body_drives_and_follows_new_device():
    core, clock = make()
    beat(core, clock, "hermes", 12, batt=15)
    assert core.drives.active() == ["battery_low"]
    core.connect("laptop")
    beat(core, clock, "laptop", batt=90)
    clock.advance(1)
    events = core.activate("laptop", "rest")
    assert summary(events) == [
        ("drive.exit", "hermes", "battery_low"),
        ("device.switch", "laptop", "laptop"),
    ]
    assert events[0].data == {"drive": "battery_low", "reason": "device_switch"}
    assert events[1].data == {"from": "hermes", "to": "laptop", "by": "rest"}
    assert core.body.batt_pct == 90 and core.self_snapshot()["self"]["mode"] == "nominal"
    # hermes dalej słabnie, ale nie jest aktywny
    assert drive_events(beat_all(core, clock, 15, hermes=5, laptop=90)) == []
    # z powrotem na hermes: licznik od zera, wejście po 10 s ciągłego warunku
    assert summary(core.activate("hermes", "rest")) == [("device.switch", "hermes", "hermes")]
    assert drive_events(beat_all(core, clock, 9, hermes=5, laptop=90)) == []
    assert summary(drive_events(beat_all(core, clock, 2, hermes=5, laptop=90))) == [
        ("drive.enter", "hermes", "battery_low"),
        ("drive.enter", "hermes", "battery_critical"),
    ]
    assert core.activate("hermes", "rest") == []  # już aktywne


def test_switch_to_known_device_without_connection_goes_offline():
    core, clock = make()
    beat(core, clock, "hermes")
    assert summary(core.activate("phone", "rest")) == [("device.switch", "phone", "phone")]
    assert core.body.link == "unknown"
    assert summary(beat_all(core, clock, 6, hermes=71)) == [  # hermes nadaje, phone milczy
        ("link.lost", "phone", None),
        ("drive.enter", "phone", "link_lost"),
    ]
    assert core.self_snapshot()["self"]["mode"] == "offline"
    # przełączenie z urządzenia w link_lost: wyjście z powodu zmiany, hermes ma łącze
    events = core.activate("hermes", "rest")
    assert summary(events) == [("drive.exit", "phone", "link_lost"), ("device.switch", "hermes", "hermes")]
    assert core.self_snapshot()["self"]["mode"] == "nominal"


def test_switch_to_device_with_lost_link_enters_link_lost_at_once():
    core, clock = make()
    beat(core, clock, "hermes")
    core.connect("laptop")
    beat(core, clock, "laptop")
    beat_all(core, clock, 6, hermes=71)
    assert core.device_state("laptop")["link"] == "lost"
    assert summary(core.activate("laptop", "rest")) == [
        ("device.switch", "laptop", "laptop"),
        ("drive.enter", "laptop", "link_lost"),
    ]


def test_device_activate_event_switches_and_counts_as_interaction():
    core, clock = make()
    beat(core, clock, "hermes")
    core.connect("laptop")
    clock.advance(5)
    events = core.handle_event(make_event("device.activate", {}), "laptop")
    assert summary(events) == [("device.switch", "laptop", "laptop")]
    assert events[0].data["by"] == "device"
    assert core.active_device == "laptop" and core.last_interaction == clock.wall()
    clock.advance(5)
    again = core.handle_event(make_event("device.activate", {}), "laptop")
    assert [e for e in again if e.type == "device.switch"] == []  # już aktywne
    assert core.last_interaction == clock.wall()


def test_active_device_from_previous_session():
    core, clock = make(active="laptop")
    assert core.active_device == "laptop" and core.body.device == "laptop"
    assert core.connect("hermes") == []
    beat(core, clock, "hermes")
    assert core.active_device == "laptop" and core.body.link == "unknown"


def test_drive_events_name_the_device_except_long_silence():
    core, clock = make()
    beat(core, clock, "hermes", 12, batt=15)
    clock.advance(4 * 3600 + 1)
    events = core.tick()
    by_drive = {e.data["drive"]: e.device for e in events if e.type == "drive.enter"}
    assert by_drive == {"link_lost": "hermes", "long_silence": None}


def test_prompt_slice_marks_device_change_and_resets_baselines():
    core, clock = make()
    beat(core, clock, "hermes", batt=40)
    first = core.prompt_slice()
    assert "device_change" not in first  # przejęcie stanu przez pierwsze urządzenie
    assert first["body_changes"]["batt_pct"] == {"from": None, "to": 40}
    core.connect("laptop")
    beat(core, clock, "laptop", batt=42)
    core.activate("laptop", "rest")
    s = core.prompt_slice()
    assert s["device_change"] == {"from": "hermes", "to": "laptop"}
    assert s["body_changes"]["batt_pct"] == {"from": None, "to": 42}  # nie "40 -> 42"
    # nieudane zapytanie: następny wycinek powtarza zmianę urządzenia
    assert core.rollback_prompt_slice()
    again = core.prompt_slice()
    assert again["device_change"] == {"from": "hermes", "to": "laptop"} and "batt_pct" in again["body_changes"]
    assert "device_change" not in core.prompt_slice()
    # tam i z powrotem między wycinkami: urządzenie to samo co w ostatnim wycinku
    core.activate("hermes", "rest")
    core.activate("laptop", "rest")
    assert "device_change" not in core.prompt_slice()


def test_switch_restarts_hold_timers_of_inactive_drives():
    core, clock = make()
    core.connect("hermes")
    core.connect("laptop")
    beat_all(core, clock, 6, hermes=15, laptop=15)  # licznik battery_low biegnie od 6 s
    assert core.drives.active() == []
    core.activate("laptop", "rest")
    # bez zerowania licznika wejście byłoby po 4 s; po przełączeniu liczy się od nowa 10 s
    assert drive_events(beat_all(core, clock, 9, hermes=15, laptop=15)) == []
    assert summary(drive_events(beat_all(core, clock, 2, hermes=15, laptop=15))) == [
        ("drive.enter", "laptop", "battery_low"),
    ]
