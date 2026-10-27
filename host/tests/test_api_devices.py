"""Wiele urządzeń w warstwie api: połączenia, przełączanie, akcje, głos, REST (docs/spec.md, sekcje 1, 10)."""

import asyncio
import json
import logging
import struct

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from host.api.main import create_app
from host.api.service import HostService, NotConnected, UnknownDevice
from host.brain import Assistant
from host.core.clock import ManualClock
from host.core.config import Config
from host.store import Store
from host.tests.audio_helpers import opus_packets
from host.tests.brain_helpers import ScriptedEngine
from host.tests.test_api_service import FakeWS, frame, hb, settle
from host.tests.test_api_voice import LLM_CFG, say, until
from host.tests.test_brain_voice import FakeSTT, FakeTTS

run = asyncio.run


def make(config=None, store=None):
    clock = ManualClock()
    service = HostService(config or Config(), clock, store or Store(clock=clock))
    service.start()
    return service, clock, service.store


async def connect(service, device=None):
    ws = FakeWS()
    task = asyncio.create_task(service.serve_connection(ws, device))
    await settle()
    return ws, task


def actions(ws, type_=None):
    sent = [json.loads(t) for t in ws.sent]
    return [a for a in sent if type_ is None or a["type"] == type_]


def active_flags(ws):
    return [a["body"]["active"] for a in actions(ws, "device.active")]


def test_devices_connect_side_by_side_and_only_the_first_is_active():
    async def scenario():
        service, _, store = make()
        hermes, t1 = await connect(service)  # bez parametru: hermes
        laptop, t2 = await connect(service, "laptop")
        assert service.core.active_device == "hermes" and store.active_device() == "hermes"
        assert active_flags(hermes) == [] and active_flags(laptop) == [False]
        health = service.health()
        assert health["device"] == "hermes" and health["devices_connected"] == 2 and health["link"]["connected"]
        # zdarzenia laptopa: zapisane z device, ale nie zmieniają body
        laptop.feed_text(hb(5))
        laptop.feed_text(frame("button", {"id": "b1", "kind": "press"}))
        hermes.feed_text(hb(80))
        await settle()
        assert service.core.body.batt_pct == 80
        assert [(e.type, e.device) for e in store.events(types=["button", "device.switch"])] == [
            ("device.switch", "hermes"), ("button", "laptop"),
        ]
        assert [e.device for e in store.events(types=["action.sent"])] == ["laptop"]
        for ws in (hermes, laptop):
            ws.disconnect()
        await asyncio.gather(t1, t2)
        assert service.health()["devices_connected"] == 0
        assert [d.id for d in store.devices()] == ["hermes", "laptop"]

    run(scenario())


def test_close_codes_for_bad_id_too_many_devices_and_replacement():
    async def scenario():
        service, _, _ = make(Config.model_validate({"devices": {"max_connected": 2}}))
        bad = FakeWS()
        await service.serve_connection(bad, "Pi_Kuchnia")
        assert bad.accepted and bad.closed == (4002, "bad device")
        a, ta = await connect(service, "a")
        b, tb = await connect(service, "b")
        c = FakeWS()
        await service.serve_connection(c, "c")
        assert c.closed == (4001, "too many devices")
        b2, tb2 = await connect(service, "b")  # to samo urządzenie: zastępuje, limit nie dotyczy
        assert b.closed == (4000, "replaced") and b2.closed is None and a.closed is None
        await tb
        assert service.health()["devices_connected"] == 2
        for ws in (a, b2):
            ws.disconnect()
        await asyncio.gather(ta, tb2)

    run(scenario())


def test_switch_by_rest_and_by_device_event_notifies_both_devices():
    async def scenario():
        service, _, store = make()
        hermes, t1 = await connect(service, "hermes")
        laptop, t2 = await connect(service, "laptop")
        await service.activate_device("laptop")
        await settle()
        assert service.core.active_device == "laptop" and store.active_device() == "laptop"
        assert active_flags(hermes) == [False] and active_flags(laptop) == [False, True]
        switch = store.events(types=["device.switch"])[-1]
        assert switch.data == {"from": "hermes", "to": "laptop", "by": "rest"} and switch.device == "laptop"
        # akcje bez device idą do aktywnego
        await service.send_action("ping", {}, wait_ack=False)
        assert actions(laptop, "ping") and not actions(hermes, "ping")
        # przycisk "używaj tego urządzenia" na hermes
        hermes.feed_text(frame("device.activate", {}))
        await settle()
        assert service.core.active_device == "hermes"
        assert store.events(types=["device.switch"])[-1].data["by"] == "device"
        assert active_flags(hermes) == [False, True] and active_flags(laptop) == [False, True, False]
        await service.activate_device("hermes")  # już aktywne: bez zdarzenia i bez akcji
        await settle()
        assert len(store.events(types=["device.switch"])) == 3 and active_flags(hermes) == [False, True]
        with pytest.raises(UnknownDevice):
            await service.activate_device("phone")
        with pytest.raises(ValueError):
            await service.activate_device("Phone")
        for ws in (hermes, laptop):
            ws.disconnect()
        await asyncio.gather(t1, t2)

    run(scenario())


def test_acks_are_matched_with_the_device_that_got_the_action(caplog):
    async def scenario():
        service, _, store = make()
        hermes, t1 = await connect(service, "hermes")
        laptop, t2 = await connect(service, "laptop")
        pending = asyncio.create_task(service.send_action("ping", {}, device="laptop"))
        await settle()
        ref = actions(laptop, "ping")[0]["id"]
        hermes.feed_text(frame("ping", {"ref": ref, "ok": True}, t="ack"))
        await settle()
        assert not pending.done()
        laptop.feed_text(frame("ping", {"ref": ref, "ok": True}, t="ack"))
        _, ack = await asyncio.wait_for(pending, 1)
        assert ack.ok
        assert [e.device for e in store.events(types=["action.ack"])] == ["hermes", "laptop"]
        with pytest.raises(NotConnected):
            await service.send_action("ping", {}, device="phone")
        for ws in (hermes, laptop):
            ws.disconnect()
        await asyncio.gather(t1, t2)

    with caplog.at_level(logging.WARNING):
        run(scenario())
    assert any("od innego urządzenia" in r.getMessage() for r in caplog.records)


def test_active_device_is_remembered_after_restart(tmp_path):
    async def first_run(path):
        service, _, store = make(store=Store(path))
        a, ta = await connect(service, "hermes")
        b, tb = await connect(service, "laptop")
        await service.activate_device("laptop")
        for ws in (a, b):
            ws.disconnect()
        await asyncio.gather(ta, tb)
        service.stop()
        store.close()

    async def second_run(path):
        service, _, store = make(store=Store(path))
        assert service.core.active_device == "laptop"
        hermes, t = await connect(service, "hermes")
        assert active_flags(hermes) == [False]  # hermes nie przejmuje roli tylko dlatego, że jest pierwszy
        assert service.health()["device"] == "laptop" and not service.health()["link"]["connected"]
        hermes.disconnect()
        await t
        store.close()

    path = tmp_path / "iris.db"
    run(first_run(path))
    run(second_run(path))


def test_audio_from_inactive_device_is_ignored():
    async def scenario():
        got = []
        clock = ManualClock()
        service = HostService(Config(), clock, Store(clock=clock), on_audio=got.append)
        service.start()
        hermes, t1 = await connect(service, "hermes")
        laptop, t2 = await connect(service, "laptop")
        laptop.feed_bytes(struct.pack(">BHH", 1, 1, 0) + b"opus")
        hermes.feed_bytes(struct.pack(">BHH", 1, 2, 0) + b"opus")
        await settle()
        assert [(f.id, f.seq) for f in got] == [(2, 0)]
        for ws in (hermes, laptop):
            ws.disconnect()
        await asyncio.gather(t1, t2)

    run(scenario())


# --- głos ---


def voice_service(tts_delay=0.0):
    clock = ManualClock()
    cfg = Config.model_validate(
        {"brain": {"audio": {"serialize_playback": False, "error_clip": "error"}}, "api": {"ack_timeout_s": 0.3}}
    )
    service = HostService(
        cfg, clock, Store(clock=clock),
        assistant=Assistant(ScriptedEngine(["Dobrze."]), LLM_CFG),
        stt=FakeSTT(), tts=FakeTTS(0.4, delay=tts_delay),
    )
    service.start()
    return service


def test_inactive_device_is_not_listened_to():
    async def scenario():
        service = voice_service()
        hermes, t1 = await connect(service, "hermes")
        laptop, t2 = await connect(service, "laptop")
        say(laptop, 1, opus_packets(440, 1.0))
        await settle(50)
        assert service.stt.calls == [] and not service._voice_tasks
        for ws in (hermes, laptop):
            ws.disconnect()
        await asyncio.gather(t1, t2)

    run(scenario())


def test_inactive_device_cannot_end_the_active_devices_utterance():
    async def scenario():
        service = voice_service()
        hermes, t1 = await connect(service, "hermes")
        laptop, t2 = await connect(service, "laptop")
        hermes.feed_text(frame("audio.utterance.start", {"utt_id": 1}))
        for seq, p in enumerate(opus_packets(440, 1.0)):
            hermes.feed_bytes(struct.pack(">BHH", 1, 1, seq) + p)
        laptop.feed_text(frame("audio.utterance.end", {"utt_id": 1, "dur_ms": 1000}))  # ten sam utt_id
        await settle(50)
        assert not service._voice_tasks and service.stt.calls == []  # żadna tura nie ruszyła
        hermes.feed_text(frame("audio.utterance.end", {"utt_id": 1, "dur_ms": 1000}))
        while not service.stt.calls:
            await asyncio.sleep(0.01)
        await asyncio.gather(*list(service._voice_tasks))
        assert len(service.stt.calls) == 1
        for ws in (hermes, laptop):
            ws.disconnect()
        await asyncio.gather(t1, t2)

    run(scenario())


def test_reply_is_dropped_when_device_switches_during_the_turn(caplog):
    async def scenario():
        service = voice_service(tts_delay=0.3)
        hermes, t1 = await connect(service, "hermes")
        laptop, t2 = await connect(service, "laptop")
        say(hermes, 1, opus_packets(440, 1.0))
        while not service.tts.calls:  # tura doszła do syntezy
            await asyncio.sleep(0.01)
        await service.activate_device("laptop")
        await asyncio.gather(*list(service._voice_tasks))
        for ws in (hermes, laptop):
            assert ws.sent_bytes == []
            assert not actions(ws, "speak") and not actions(ws, "speak.clip")
        for ws in (hermes, laptop):
            ws.disconnect()
        await asyncio.gather(t1, t2)

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert any("przełączone w trakcie" in r.getMessage() for r in caplog.records)


# --- REST ---


@pytest.fixture
def client():
    clock = ManualClock()
    app = create_app(Config(), clock=clock, store=Store(clock=clock), run_tick_loop=False)
    with TestClient(app) as c:
        yield c


def test_rest_devices_activate_and_filters(client):
    assert client.get("/devices").json() == {"active": None, "devices": []}
    with client.websocket_connect("/ws?device=laptop") as laptop:
        laptop.send_text(hb(55))
        with client.websocket_connect("/ws?device=hermes") as hermes:
            assert json.loads(hermes.receive_text())["body"] == {"active": False}
            hermes.send_text(hb(90))
            body = client.get("/devices").json()
            assert body["active"] == "laptop"
            assert [(d["id"], d["active"], d["connected"], d["link"]) for d in body["devices"]] == [
                ("hermes", False, True, "ok"), ("laptop", True, True, "ok"),
            ]
            assert body["devices"][1]["batt_pct"] == 55 and body["devices"][1]["first_seen_at"]
            r = client.post("/actions", json={"type": "ping", "device": "hermes", "wait_ack": False})
            assert r.status_code == 200 and r.json()["device"] == "hermes"
            assert json.loads(hermes.receive_text())["type"] == "ping"
            assert client.post("/devices/hermes/activate").json() == {"active": "hermes"}
            assert json.loads(hermes.receive_text())["body"] == {"active": True}
            assert json.loads(laptop.receive_text())["body"] == {"active": False}
            assert client.get("/health").json()["device"] == "hermes"
            assert client.get("/self").json()["self"]["device"] == "hermes"
            assert client.get("/self").json()["body"]["batt_pct"] == 90
        r = client.post("/actions", json={"type": "ping"})
        assert r.status_code == 409  # aktywny hermes rozłączony
    assert client.post("/devices/phone/activate").status_code == 404
    assert client.post("/devices/Phone/activate").status_code == 422
    events = client.get("/events", params={"device": "laptop"}).json()["events"]
    assert {e["device"] for e in events} == {"laptop"}
    assert "device.switch" in [e["type"] for e in events]
    history = client.get("/self/history").json()["events"]
    assert [e["data"]["by"] for e in history if e["type"] == "device.switch"] == ["first", "rest"]


def test_rest_bad_device_id_closes_with_4002(client):
    with client.websocket_connect("/ws?device=..") as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 4002


# --- możliwości urządzeń ---


def caps_frame(**caps):
    return frame("device.caps", caps)


def test_devices_list_shows_caps():
    async def scenario():
        service, _, _ = make()
        ws, t = await connect(service, "laptop")
        assert service.devices()["devices"][0]["caps"] is None
        ws.feed_text(caps_frame(has=["mic", "speaker"], clips=["ready"], playback_bytes=8000))
        await settle()
        assert service.devices()["devices"][0]["caps"] == {
            "has": ["mic", "speaker"], "clips": ["ready"], "playback_bytes": 8000, "playback_packets": None,
        }
        ws.disconnect()
        await t

    run(scenario())


def test_reply_fits_the_smaller_buffer_of_the_device():
    async def scenario():
        service = voice_service()
        ws, t = await connect(service, "laptop")
        ws.feed_text(caps_frame(has=["mic", "speaker"], playback_packets=10))
        say(ws, 1, opus_packets(440, 1.0))
        await asyncio.gather(*await wait_tasks(service))
        assert len(ws.sent_bytes) == 10  # 10 pakietów zamiast 0,8 s mowy
        ws.disconnect()
        await t

    run(scenario())


def test_device_without_speaker_gets_no_reply_and_unknown_clip_is_not_sent():
    async def scenario():
        service = voice_service()
        ws, t = await connect(service, "laptop")
        ws.feed_text(caps_frame(has=["mic"]))
        say(ws, 1, opus_packets(440, 1.0))
        await settle(50)
        assert not service._voice_tasks and service.stt.calls == []
        # klip błędu tylko, gdy urządzenie go ma
        ws.feed_text(caps_frame(has=["mic", "speaker"], clips=["ready"]))
        await settle()
        failures = []

        async def fail_stt(pcm, rate):
            from host.brain.speech import SpeechError

            failures.append(rate)
            raise SpeechError("stt nie działa")

        service.stt.transcribe = fail_stt  # nieudana tura: klip błędu, jeśli urządzenie go ma
        say(ws, 2, opus_packets(440, 1.0))
        await until(lambda: len(failures) == 1)
        await asyncio.gather(*list(service._voice_tasks))
        assert not actions(ws, "speak.clip")
        ws.feed_text(caps_frame(has=["mic", "speaker"], clips=["error"]))
        await settle()
        say(ws, 3, opus_packets(440, 1.0))
        await until(lambda: actions(ws, "speak.clip"))
        assert [a["body"] for a in actions(ws, "speak.clip")] == [{"clip": "error"}]
        ws.disconnect()
        await t

    run(scenario())


async def wait_tasks(service, timeout=3.0):
    """Czeka, aż ruszy tura głosowa, i zwraca jej zadania."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not service._voice_tasks and loop.time() < end:
        await asyncio.sleep(0.01)
    assert service._voice_tasks, "tura głosowa nie ruszyła"
    return list(service._voice_tasks)
