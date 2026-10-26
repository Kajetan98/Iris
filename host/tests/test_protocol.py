import json

import pytest

from host.core.protocol import (
    BodyHeartbeat,
    Button,
    DecodedAck,
    DecodedEvent,
    FrameRejected,
    decode_frame,
    encode_action,
)


def frame(type_, body, *, t="event", v=1, id_="1", ts=1000):
    return json.dumps({"v": v, "t": t, "id": id_, "ts": ts, "type": type_, "body": body})


def test_decodes_heartbeat():
    ev = decode_frame(frame("body.heartbeat", {"batt_pct": 71, "link_rssi": -58}))
    assert isinstance(ev, DecodedEvent)
    assert isinstance(ev.body, BodyHeartbeat)
    assert ev.type == "body.heartbeat"
    assert ev.body.batt_pct == 71
    assert ev.body.frame_errors is None


def test_accepts_bytes():
    ev = decode_frame(frame("button", {"id": "b1", "kind": "long"}).encode("utf-8"))
    assert isinstance(ev.body, Button)
    assert ev.body.kind == "long"


def test_ignores_extra_fields():
    ev = decode_frame(
        json.dumps(
            {
                "v": 1, "t": "event", "id": "1", "ts": 1, "type": "button",
                "body": {"id": "b1", "kind": "press", "future": 1}, "extra": True,
            }
        )
    )
    assert ev.body.id == "b1"


def test_sensor_fields_are_optional():
    ev = decode_frame(frame("body.sensor", {"temp_c": 24.1}))
    assert ev.body.temp_c == 24.1
    assert ev.body.presence is None


def test_decodes_ack():
    ack = decode_frame(frame("ping", {"ref": "a7", "ok": False, "err": "bad pin"}, t="ack"))
    assert isinstance(ack, DecodedAck)
    assert (ack.body.ref, ack.body.ok, ack.body.err) == ("a7", False, "bad pin")


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ("not json", "JSON"),
        ("[1,2]", "obiektem"),
        (b"\xff\xfe", "UTF-8"),
        (frame("body.heartbeat", {"batt_pct": 50, "link_rssi": -50}, v=2), "wersja"),
        (frame("unknown.type", {}), "nieznany typ"),
        (frame("body.heartbeat", {"link_rssi": -50}), "batt_pct"),
        (frame("body.heartbeat", {"batt_pct": 101, "link_rssi": -50}), "batt_pct"),
        (frame("button", {"id": "b", "kind": "double"}), "kind"),
        (frame("speak", {"audio_id": 1}, t="action"), "akcji"),
        (frame("ping", {"ok": True}, t="ack"), "ack"),
        (json.dumps({"v": 1, "t": "event", "type": "button", "body": {}}), "koperta"),
    ],
)
def test_rejects(raw, fragment):
    with pytest.raises(FrameRejected) as e:
        decode_frame(raw)
    assert fragment in e.value.reason


def test_encode_action_builds_envelope():
    out = json.loads(encode_action("haptic", {"pattern": "warn"}, id="h1", ts_ms=42))
    assert out == {
        "v": 1, "t": "action", "id": "h1", "ts": 42, "type": "haptic",
        "body": {"pattern": "warn"},
    }


def test_encode_action_ping_has_empty_body():
    out = json.loads(encode_action("ping", {}, id="p", ts_ms=0))
    assert out["body"] == {}


def test_encode_action_rejects_unknown_type():
    with pytest.raises(ValueError, match="nieznany typ"):
        encode_action("self.destruct", {}, id="x", ts_ms=0)


def test_encode_action_rejects_bad_body():
    with pytest.raises(ValueError, match="pattern"):
        encode_action("haptic", {"pattern": "loud"}, id="x", ts_ms=0)
