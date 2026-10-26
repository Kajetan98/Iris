"""Model ciała z sim/. Zgodność ramek ze spec sprawdza dekoder hosta."""

import json
import struct

import pytest

from host.core.protocol import (
    AckBody,
    BodyFault,
    BodyHeartbeat,
    BodySensor,
    Button,
    DecodedAck,
    DecodedEvent,
    SystemBoot,
    decode_audio_frame,
    decode_frame,
)
from sim.model import AUDIO_HEADER, BodyModel, SimConfig, parse_script


def texts(model):
    return [o.data for o in model.take_out() if isinstance(o.data, str)]


def decoded(model):
    return [decode_frame(t) for t in texts(model)]


def make(**kw):
    return BodyModel(SimConfig(**kw), 0.0)


def action(type_, body, id_="h1"):
    return json.dumps({"v": 1, "t": "action", "id": id_, "ts": 1, "type": type_, "body": body})


def ack_of(model, now=1.0):
    (ack,) = [d for d in decoded(model) if isinstance(d, DecodedAck)]
    return ack


# --- start i heartbeat ---


def test_boot_frame_matches_spec():
    m = make()
    (boot,) = decoded(m)
    assert isinstance(boot.body, SystemBoot)
    assert boot.envelope.ts == 0
    assert boot.body.fw == "sim-0.1" and boot.body.reset_reason == "power"


def test_heartbeat_cadence_and_fields():
    m = make(batt_pct=64, rssi=-70)
    m.take_out()
    m.tick(0.0)
    hbs = [d for d in decoded(m) if d.type == "body.heartbeat"]
    assert len(hbs) == 1 and isinstance(hbs[0].body, BodyHeartbeat)
    assert (hbs[0].body.batt_pct, hbs[0].body.link_rssi, hbs[0].body.frame_errors) == (64, -70, 0)
    m.tick(0.5)
    assert not [d for d in decoded(m) if d.type == "body.heartbeat"]
    m.tick(1.0)
    hb = [d for d in decoded(m) if d.type == "body.heartbeat"]
    assert len(hb) == 1 and hb[0].envelope.ts == 1000


def test_long_pause_gives_one_heartbeat_not_a_burst():
    m = make()
    m.tick(0.0)
    m.take_out()
    m.tick(30.0)
    assert len([d for d in decoded(m) if d.type == "body.heartbeat"]) == 1


def test_ids_are_unique_and_heartbeats_not_bufferable():
    m = make()
    m.tick(0.0)
    m.tick(1.0)
    outs = m.take_out()
    ids = [json.loads(o.data)["id"] for o in outs]
    assert len(ids) == len(set(ids))
    assert all(not o.bufferable for o in outs if '"body.heartbeat"' in o.data)
    assert all(o.bufferable for o in outs if '"system.boot"' in o.data or '"body.sensor"' in o.data)


def test_battery_drain_and_clamp():
    m = make(batt_pct=50, drain_per_min=6)
    m.tick(0.0)
    m.tick(60.0)
    assert m.batt == pytest.approx(44)
    m.tick(6000.0)
    assert m.batt == 0.0
    m.drain = -60
    m.tick(6600.0)
    assert m.batt == 100.0


def test_battery_reported_as_whole_number_when_whole():
    m = make(batt_pct=71.0)
    m.take_out()
    m.tick(0.0)
    hb = next(t for t in texts(m) if "heartbeat" in t)
    assert '"batt_pct":71,' in hb


# --- temperatura i sensory ---


def test_temperature_sent_on_change_or_period():
    m = make(temp_c=24.0, sensor_period_s=30)
    m.tick(0.0)
    assert [d.body.temp_c for d in decoded(m) if isinstance(d.body, BodySensor)] == [24.0]
    m.temp = 24.3
    m.tick(1.0)
    assert not [d for d in decoded(m) if isinstance(d.body, BodySensor)]
    m.temp = 24.6
    m.tick(2.0)
    assert [d.body.temp_c for d in decoded(m) if isinstance(d.body, BodySensor)] == [24.6]
    m.tick(31.9)
    assert not [d for d in decoded(m) if isinstance(d.body, BodySensor)]
    m.tick(32.0)
    assert len([d for d in decoded(m) if isinstance(d.body, BodySensor)]) == 1


def test_temp_fault_stops_temperature_and_clear_resumes():
    m = make()
    m.tick(0.0)
    m.take_out()
    assert "usterka" in m.command("fault temp stuck", 1.0)
    (f,) = [d for d in decoded(m) if d.type == "body.fault"]
    assert isinstance(f.body, BodyFault) and (f.body.sensor, f.body.code) == ("temp", "stuck")
    m.temp = 30
    m.tick(2.0)
    assert not [d for d in decoded(m) if d.type == "body.sensor"]
    m.command("clear temp", 3.0)
    m.tick(3.0)
    types = [d.type for d in decoded(m)]
    assert "body.fault.clear" in types and "body.sensor" in types


def test_clear_without_fault_and_bad_sensor():
    m = make()
    assert "nie ma usterki" in m.command("clear gps", 0.0)
    assert "nieznany sensor" in m.command("fault lidar", 0.0)
    assert "fault <sensor>" in m.command("fault", 0.0)


# --- komendy ---


def test_press_and_local_and_presence_and_gps():
    m = make()
    m.take_out()
    m.command("press", 1.0)
    m.command("press long", 1.0)
    m.command("local volume_up", 1.0)
    m.command("presence on", 1.0)
    m.command("gps 52.1 21.0", 1.0)
    d = decoded(m)
    assert [(x.body.id, x.body.kind) for x in d[:2]] == [("b1", "press"), ("b1", "long")]
    assert isinstance(d[0].body, Button)
    assert (d[2].body.cmd, d[2].body.result) == ("volume_up", "ok")
    assert d[3].body.presence is True
    assert d[4].body.gps == {"lat": 52.1, "lon": 21.0}


def test_gps_fault_blocks_position():
    m = make()
    m.take_out()
    m.command("fault gps no_fix", 0.0)
    m.take_out()
    assert "usterkę" in m.command("gps 1 2", 0.0)
    assert m.take_out() == []


def test_utterance_stream_follows_spec():
    m = make(audio_frame_ms=20, audio_frame_bytes=8)
    m.take_out()
    m.command("say 100", 2.0)
    outs = m.take_out()
    kinds = ["bin" if isinstance(o.data, bytes) else json.loads(o.data)["type"] for o in outs]
    assert kinds == ["audio.utterance.start"] + ["bin"] * 5 + ["audio.utterance.end"]
    start, end = decode_frame(outs[0].data), decode_frame(outs[-1].data)
    assert start.body.utt_id == end.body.utt_id and end.body.dur_ms == 100
    frames = [decode_audio_frame(o.data) for o in outs[1:-1]]
    assert [f.seq for f in frames] == [0, 1, 2, 3, 4]
    assert {f.id for f in frames} == {start.body.utt_id} and {f.kind for f in frames} == {1}
    assert all(len(f.data) == 8 for f in frames)
    assert all(not o.bufferable for o in outs[1:-1])  # audio nie jest buforowane


def test_utterance_ids_increase_and_mic_fault_blocks():
    m = make()
    m.take_out()
    m.command("say", 0.0)
    m.command("say", 0.0)
    ids = [d.body.utt_id for d in decoded(m) if d.type == "audio.utterance.start"]
    assert ids == [1, 2]
    m.command("fault mic", 0.0)
    m.take_out()
    assert "usterkę" in m.command("say", 0.0)
    assert m.take_out() == []


def test_reboot_resets_ts_boot_id_and_faults():
    m = make()
    m.command("fault imu", 1.0)
    first = m.boot_id
    m.take_out()
    m.command("reboot watchdog", 50.0)
    (boot,) = [d for d in decoded(m) if d.type == "system.boot"]
    assert boot.body.reset_reason == "watchdog" and boot.body.boot_id != first
    assert boot.envelope.ts == 0 and not m.faults
    m.tick(50.0)
    assert [d.envelope.ts for d in decoded(m) if d.type == "body.heartbeat"] == [0]  # heartbeat zaraz po restarcie
    m.tick(51.0)
    assert [d.envelope.ts for d in decoded(m) if d.type == "body.heartbeat"] == [1000]


def test_ids_stay_unique_across_reboot():
    m = make()
    m.tick(0.0)
    ids = [json.loads(o.data)["id"] for o in m.take_out()]
    m.command("reboot", 1.0)
    ids += [json.loads(o.data)["id"] for o in m.take_out()]
    assert len(ids) == len(set(ids))


def test_drop_mute_quit_and_unknown():
    m = make()
    m.take_out()
    m.command("drop 5", 10.0)
    assert m.wants_disconnect and m.offline_until == 15.0
    m.command("mute 3", 10.0)
    m.tick(10.0)
    m.tick(11.0)
    assert not [d for d in decoded(m) if d.type in ("body.heartbeat", "body.sensor")]
    m.tick(13.0)
    assert [d for d in decoded(m) if d.type == "body.heartbeat"]
    assert "nieznana" in m.command("dance", 0.0)
    assert m.command("", 0.0) is None
    m.command("quit", 0.0)
    assert m.quit_requested


@pytest.mark.parametrize("line", ["batt 101", "batt -1", "batt x", "temp", "drain a b", "say 0", "say abc", "presence maybe", "gps 1", "local"])
def test_bad_command_arguments_are_reported_not_raised(line):
    m = make()
    m.take_out()
    assert m.command(line, 0.0).startswith("błąd")
    assert m.take_out() == []  # błędna komenda nie wysyła nic


def test_status_mentions_key_fields():
    m = make()
    s = m.command("status", 0.0)
    assert m.boot_id in s and "bateria" in s and "bufor" in s


# --- akcje od hosta ---


def test_ping_ack():
    m = make()
    m.take_out()
    m.on_host_text(action("ping", {}, "h7"), 1.0)
    ack = ack_of(m)
    assert isinstance(ack.body, AckBody) and ack.body.ref == "h7" and ack.body.ok and ack.body.err is None
    assert ack.envelope.type == "ping"


def test_haptic_valid_and_invalid():
    m = make()
    m.take_out()
    m.on_host_text(action("haptic", {"pattern": "warn"}, "h1"), 1.0)
    assert ack_of(m).body.ok and m.haptic_log == ["warn"]
    m.on_host_text(action("haptic", {"pattern": "loud"}, "h2"), 1.0)
    ack = ack_of(m)
    assert not ack.body.ok and "pattern" in ack.body.err and m.haptic_log == ["warn"]


def test_gpio_whitelist_and_values():
    m = make(gpio_pins=frozenset({5}))
    m.take_out()
    for body, ok in [({"pin": 5, "value": 1}, True), ({"pin": 9, "value": 1}, False),
                     ({"pin": 5, "value": 2}, False), ({"pin": 5}, False), ({"pin": True, "value": 1}, False)]:
        m.on_host_text(action("gpio.set", body), 1.0)
        assert ack_of(m).body.ok is ok, body
    assert m.gpio == {5: 1}


def test_config_set_rules():
    m = make()
    m.take_out()
    for body, ok in [({"key": "volume", "value": 80}, True), ({"key": "volume", "value": 101}, False),
                     ({"key": "color", "value": 1}, False), ({"key": "volume", "value": "loud"}, False),
                     ({"key": "temp_warn_c", "value": 38.5}, True)]:
        m.on_host_text(action("config.set", body), 1.0)
        assert ack_of(m).body.ok is ok, body
    assert m.settings["volume"] == 80 and m.settings["temp_warn_c"] == 38.5


def test_speak_requires_received_audio():
    m = make()
    m.take_out()
    m.on_host_text(action("speak", {"audio_id": 7}), 1.0)
    assert not ack_of(m).body.ok
    m.on_host_binary(struct.pack(">BHH", 2, 7, 0) + b"abcd")
    m.on_host_binary(struct.pack(">BHH", 2, 7, 1) + b"ef")
    m.on_host_text(action("speak", {"audio_id": 7}), 1.0)
    assert ack_of(m).body.ok and m.played == [(7, 6)]
    m.on_host_text(action("speak", {"audio_id": 7}), 1.0)  # bufor zużyty
    assert not ack_of(m).body.ok


def test_speak_accepts_numeric_string_and_rejects_garbage():
    m = make()
    m.take_out()
    m.on_host_binary(AUDIO_HEADER.pack(2, 3, 0) + b"x")
    m.on_host_text(action("speak", {"audio_id": "3"}), 1.0)
    assert ack_of(m).body.ok
    for body in ({}, {"audio_id": "abc"}, {"audio_id": None}, {"audio_id": True}):
        m.on_host_text(action("speak", body), 1.0)
        assert not ack_of(m).body.ok, body


def test_clip_known_and_unknown():
    m = make()
    m.take_out()
    m.on_host_text(action("speak.clip", {"clip": "ready"}), 1.0)
    assert ack_of(m).body.ok
    m.on_host_text(action("speak.clip", {"clip": "nope"}), 1.0)
    assert not ack_of(m).body.ok and m.clips_played == ["ready"]


def test_action_outside_list_is_rejected_with_ack():
    m = make()
    m.take_out()
    m.on_host_text(action("self.destruct", {}, "h9"), 1.0)
    ack = ack_of(m)
    assert (ack.body.ref, ack.body.ok, ack.body.err) == ("h9", False, "unknown action")


def test_malformed_host_frames_count_as_frame_errors():
    m = make()
    m.take_out()
    m.on_host_text("{zepsute", 1.0)
    m.on_host_text(json.dumps({"v": 2, "t": "action", "id": "x", "type": "ping"}), 1.0)
    m.on_host_text(json.dumps({"v": 1, "t": "event", "id": "x", "type": "ping"}), 1.0)
    m.on_host_text(json.dumps({"v": 1, "t": "action", "type": "ping"}), 1.0)
    m.on_host_text(json.dumps({"v": 1, "t": "action", "id": "x", "type": "ping", "body": [1]}), 1.0)
    m.on_host_binary(b"\x02\x00")
    m.on_host_binary(struct.pack(">BHH", 1, 1, 0) + b"x")  # mikrofon nie przychodzi od hosta
    assert m.frame_errors == 7
    assert m.take_out() == []  # na ramki niezgodne ze spec nie odpowiada
    m.tick(10.0)
    hb = next(d for d in decoded(m) if d.type == "body.heartbeat")
    assert hb.body.frame_errors == 7


# --- bufor ---


def test_buffer_keeps_events_in_order_and_drops_oldest():
    m = make(buffer_limit=3)
    m.take_out()
    for i in range(5):
        m.command(f"local c{i}", float(i))
    m.tick(5.0)
    m.stash(m.take_out())  # lokalne komendy + heartbeat + sensor
    kept = [json.loads(o.data) for o in m.take_buffer()]
    assert len(kept) == 3
    assert m.dropped > 0
    assert all(f["type"] != "body.heartbeat" for f in kept)


def test_acks_are_not_buffered():
    m = make()
    m.take_out()
    m.on_host_text(action("ping", {}), 1.0)
    m.stash(m.take_out())
    assert list(m.buffer) == []


def test_requeue_puts_events_back_in_front():
    m = make(buffer_limit=4)
    m.take_out()
    m.command("local a", 0.0)
    m.command("local b", 0.0)
    a, b = m.take_out()
    m.stash([b])
    m.requeue([a])
    assert [json.loads(o.data)["body"]["cmd"] for o in m.take_buffer()] == ["a", "b"]


# --- skrypt ---


def test_parse_script():
    steps = parse_script("# scenariusz\n10 batt 15  # spadek\n\n2.5 press\n")
    assert steps == [(2.5, "press"), (10.0, "batt 15")]


@pytest.mark.parametrize("text", ["abc batt 1", "5", "-1 press"])
def test_parse_script_errors(text):
    with pytest.raises(ValueError, match="linia 1"):
        parse_script(text)
