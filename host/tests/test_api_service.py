import asyncio
import json
import logging
import sqlite3
import struct
from datetime import timedelta

import pytest

from host.api.service import AckTimeout, HostService, NotConnected
from host.core.clock import ManualClock
from host.core.config import Config
from host.store import Store


def frame(type_, body, t="event", id_="1", ts=500):
    return json.dumps({"v": 1, "t": t, "id": id_, "ts": ts, "type": type_, "body": body})


def hb(batt=71):
    return frame("body.heartbeat", {"batt_pct": batt, "link_rssi": -58})


class FakeWS:
    def __init__(self):
        self.sent = []
        self.sent_bytes = []
        self.log = []  # ("text" | "bytes", dane) w kolejności wysyłki
        self.closed = None
        self.accepted = False
        self._q = asyncio.Queue()

    async def accept(self):
        self.accepted = True

    async def receive(self):
        return await self._q.get()

    async def send_text(self, data):
        if self.closed:
            raise RuntimeError("socket zamknięty")
        self.sent.append(data)
        self.log.append(("text", data))

    async def send_bytes(self, data):
        if self.closed:
            raise RuntimeError("socket zamknięty")
        self.sent_bytes.append(data)
        self.log.append(("bytes", data))

    async def close(self, code=1000, reason=None):
        self.closed = (code, reason)
        self._q.put_nowait({"type": "websocket.disconnect", "code": code})

    def feed_text(self, s):
        self._q.put_nowait({"type": "websocket.receive", "text": s})

    def feed_bytes(self, b):
        self._q.put_nowait({"type": "websocket.receive", "bytes": b})

    def disconnect(self):
        self._q.put_nowait({"type": "websocket.disconnect", "code": 1000})


async def settle(n=10):
    for _ in range(n):
        await asyncio.sleep(0)


def make(config=None):
    clock = ManualClock()
    store = Store(clock=clock)
    service = HostService(config or Config(), clock, store)
    service.start()
    return service, clock, store


def types(store, **kw):
    return [e.type for e in store.events(**kw)]


# --- start / stop ---


def test_start_records_host_start_and_snapshot():
    service, _, store = make()
    assert types(store) == ["host.start"]
    assert len(store.snapshots()) == 1


def test_stop_records_host_stop_snapshot_and_ends_session():
    service, clock, store = make()
    clock.advance(5)
    service.stop()
    assert types(store)[-1] == "host.stop"
    assert len(store.snapshots()) == 2
    assert store.sessions()[0].ended_at is not None


# --- ramki tekstowe ---


def test_heartbeats_update_state_but_are_not_stored_by_default():
    service, _, store = make()
    service.handle_text(hb(64))
    assert service.core.body.snapshot()["batt_pct"] == 64
    assert "body.heartbeat" not in types(store)


def test_heartbeats_stored_when_enabled():
    service, _, store = make(Config.model_validate({"api": {"store_heartbeats": True}}))
    service.handle_text(hb())
    assert types(store, types=["body.heartbeat"]) == ["body.heartbeat"]


def test_body_events_carry_boot_id_and_body_ts():
    service, _, store = make()
    service.handle_text(frame("system.boot", {"boot_id": "b9", "fw": "0.1", "reset_reason": "power"}, ts=10))
    service.handle_text(frame("body.sensor", {"temp_c": 22.5}, ts=2500))
    boot, sensor = store.events(types=["system.boot", "body.sensor"])
    assert boot.boot_id == "b9" and boot.ts_ms == 10
    assert sensor.boot_id == "b9" and sensor.ts_ms == 2500
    assert sensor.data["temp_c"] == 22.5


def test_rejected_frames_are_counted_logged_and_not_stored(caplog):
    service, _, store = make()
    with caplog.at_level(logging.WARNING):
        service.handle_text("nie json")
        service.handle_text(frame("teleport", {}))
        service.handle_text(json.dumps({"v": 7, "t": "event", "id": "1", "ts": 0, "type": "button", "body": {}}))
    assert service.rejected_frames == 3
    assert len(caplog.records) == 3
    assert types(store) == ["host.start"]


def test_drive_change_is_stored_with_snapshot():
    service, clock, store = make()
    for _ in range(12):
        clock.advance(1)
        service.handle_text(hb(18))
    assert types(store, types=["drive.enter"]) == ["drive.enter"]
    assert len(store.snapshots()) == 2  # start + wejście w battery_low
    assert store.latest_snapshot().self_state["mode"] == "saving"


def test_tick_records_link_loss():
    service, clock, store = make()
    service.handle_text(hb())
    clock.advance(6)
    service.tick()
    assert types(store, types=["link.lost", "drive.enter"]) == ["link.lost", "drive.enter"]
    assert store.events(types=["drive.enter"])[0].data["drive"] == "link_lost"


def beat_for(service, clock, seconds):
    """Heartbeat i tick co sekundę, jak w działającym hoście."""
    for _ in range(seconds):
        clock.advance(1)
        service.handle_text(hb())
        service.tick()


def test_tick_touches_session_and_snapshots_periodically():
    service, clock, store = make()
    start = clock.wall()
    beat_for(service, clock, 9)
    # jedyny zapis przed touch: device.switch przy pierwszym heartbeacie (t = 1 s)
    assert store.sessions()[0].last_seen_at == start + timedelta(seconds=1)
    beat_for(service, clock, 1)
    assert store.sessions()[0].last_seen_at == start + timedelta(seconds=10)
    assert len(store.snapshots()) == 1
    beat_for(service, clock, 290)  # t = 300 s
    assert len(store.snapshots()) == 2
    beat_for(service, clock, 10)
    assert len(store.snapshots()) == 2  # następna dopiero po kolejnych 300 s


def test_store_failure_does_not_stop_the_core(monkeypatch, caplog):
    service, clock, store = make()

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "append_host_event", boom)
    monkeypatch.setattr(store, "append_body_event", boom)
    with caplog.at_level(logging.ERROR):
        service.handle_text(frame("body.sensor", {"temp_c": 30}))
        clock.advance(6)
        service.tick()  # link.lost nie może się zapisać
    assert service.core.body.snapshot()["temp_c"] == 30
    assert service.core.body.link == "lost"
    assert any("zapis" in r.getMessage() for r in caplog.records)


# --- audio ---


def test_binary_frames_go_to_audio_hook_and_bad_ones_are_rejected():
    got = []
    clock = ManualClock()
    service = HostService(Config(), clock, Store(clock=clock), on_audio=got.append)
    service.start()
    service.handle_binary(struct.pack(">BHH", 1, 258, 3) + b"opus")
    assert (got[0].kind, got[0].id, got[0].seq, got[0].data) == (1, 258, 3, b"opus")
    service.handle_binary(struct.pack(">BHH", 2, 1, 1) + b"x")  # odtwarzanie nie przychodzi od ciała
    service.handle_binary(b"\x01\x00")  # krótsza niż nagłówek
    assert len(got) == 1
    assert service.rejected_frames == 2


# --- połączenie ---


def test_connection_loop_dispatches_frames_and_survives_bad_ones():
    async def scenario():
        service, _, store = make()
        ws = FakeWS()
        task = asyncio.create_task(service.serve_connection(ws))
        await settle()
        assert ws.accepted and service.connected
        ws.feed_text("{zepsute")
        ws.feed_bytes(b"\x09")
        ws.feed_text(frame("button", {"id": "b1", "kind": "press"}))
        ws.disconnect()
        await asyncio.wait_for(task, 1)
        assert not service.connected
        assert types(store, types=["button"]) == ["button"]
        assert service.rejected_frames == 2

    asyncio.run(scenario())


def test_unexpected_error_in_handler_keeps_connection(monkeypatch, caplog):
    async def scenario():
        service, _, store = make()
        calls = []
        real = service.core.handle_event

        def flaky(ev):
            calls.append(ev.type)
            if len(calls) == 1:
                raise RuntimeError("błąd w rdzeniu")
            return real(ev)

        monkeypatch.setattr(service.core, "handle_event", flaky)
        ws = FakeWS()
        task = asyncio.create_task(service.serve_connection(ws))
        ws.feed_text(frame("button", {"id": "b", "kind": "press"}))
        ws.feed_text(frame("button", {"id": "b", "kind": "long"}))
        ws.disconnect()
        await asyncio.wait_for(task, 1)
        assert calls == ["button", "button"]

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert any("obsłudze ramki" in r.getMessage() for r in caplog.records)


def test_new_connection_replaces_the_old_one():
    async def scenario():
        service, _, _ = make()
        first, second = FakeWS(), FakeWS()
        t1 = asyncio.create_task(service.serve_connection(first))
        await settle()
        t2 = asyncio.create_task(service.serve_connection(second))
        await settle()
        assert first.closed == (4000, "replaced")
        await asyncio.wait_for(t1, 1)
        assert service.connected  # zamknięcie starego nie odłącza nowego
        second.disconnect()
        await asyncio.wait_for(t2, 1)
        assert not service.connected

    asyncio.run(scenario())


# --- akcje ---


def test_send_action_requires_connection():
    async def scenario():
        service, _, _ = make()
        with pytest.raises(NotConnected):
            await service.send_action("ping", {})

    asyncio.run(scenario())


def test_invalid_action_is_rejected_without_consuming_an_id():
    async def scenario():
        service, _, store = make()
        ws = FakeWS()
        task = asyncio.create_task(service.serve_connection(ws))
        await settle()
        with pytest.raises(ValueError):
            await service.send_action("haptic", {"pattern": "loud"})
        with pytest.raises(ValueError):
            await service.send_action("self.destruct", {})
        aid, _ = await service.send_action("ping", {}, wait_ack=False)
        assert aid == "h1"
        assert "action.sent" in types(store)
        ws.disconnect()
        await task

    asyncio.run(scenario())


def test_action_round_trip_with_ack():
    async def scenario():
        service, clock, store = make()
        ws = FakeWS()
        task = asyncio.create_task(service.serve_connection(ws))
        await settle()
        clock.advance(2.5)
        send = asyncio.create_task(service.send_action("haptic", {"pattern": "warn"}))
        await settle()
        sent = json.loads(ws.sent[0])
        assert sent == {
            "v": 1, "t": "action", "id": "h1", "ts": 2500, "type": "haptic",
            "body": {"pattern": "warn"},
        }
        ws.feed_text(frame("haptic", {"ref": "h1", "ok": True}, t="ack"))
        aid, ack = await asyncio.wait_for(send, 1)
        assert aid == "h1" and ack.ok and ack.err is None
        sent_ev, ack_ev = store.events(types=["action.sent", "action.ack"])
        assert sent_ev.data == {"id": "h1", "type": "haptic", "body": {"pattern": "warn"}}
        assert ack_ev.data == {"ref": "h1", "ok": True, "err": None}
        ws.disconnect()
        await task

    asyncio.run(scenario())


def test_ack_with_error_is_returned():
    async def scenario():
        service, _, _ = make()
        ws = FakeWS()
        task = asyncio.create_task(service.serve_connection(ws))
        await settle()
        send = asyncio.create_task(service.send_action("gpio.set", {"pin": 99, "value": 1}))
        await settle()
        ws.feed_text(frame("gpio.set", {"ref": "h1", "ok": False, "err": "pin poza listą"}, t="ack"))
        _, ack = await asyncio.wait_for(send, 1)
        assert (ack.ok, ack.err) == (False, "pin poza listą")
        ws.disconnect()
        await task

    asyncio.run(scenario())


def test_ack_timeout_and_late_ack(caplog):
    async def scenario():
        cfg = Config.model_validate({"api": {"ack_timeout_s": 0.05}})
        service, _, store = make(cfg)
        ws = FakeWS()
        task = asyncio.create_task(service.serve_connection(ws))
        await settle()
        with pytest.raises(AckTimeout) as e:
            await service.send_action("ping", {})
        assert e.value.action_id == "h1"
        assert "action.sent" in types(store)
        ws.feed_text(frame("ping", {"ref": "h1", "ok": True}, t="ack"))  # spóźnione
        await settle()
        assert "action.ack" in types(store)
        ws.disconnect()
        await task

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert any("nieznanej" in r.getMessage() for r in caplog.records)


def test_send_on_closed_socket_is_not_connected():
    async def scenario():
        service, _, _ = make()
        ws = FakeWS()
        task = asyncio.create_task(service.serve_connection(ws))
        await settle()
        ws.closed = (1006, None)  # gniazdo padło, ale pętla jeszcze tego nie widzi
        with pytest.raises(NotConnected):
            await service.send_action("ping", {})
        ws.disconnect()
        await task

    asyncio.run(scenario())


# --- health ---


def test_health_reflects_link_and_connection():
    async def scenario():
        service, clock, _ = make()
        h = service.health()
        assert h["status"] == "degraded" and h["link"]["state"] == "unknown"
        assert h["link"]["connected"] is False and h["store"] == "ok"
        ws = FakeWS()
        task = asyncio.create_task(service.serve_connection(ws))
        await settle()
        service.handle_text(hb())
        clock.advance(3)
        h = service.health()
        assert h["status"] == "ok" and h["link"]["connected"] is True
        assert h["link"]["rssi"] == -58 and h["link"]["last_seen_ms"] == 3000
        assert h["uptime_s"] == 3 and h["session_id"] == 1
        ws.disconnect()
        await task

    asyncio.run(scenario())


def test_health_reports_store_error(monkeypatch):
    service, _, store = make()

    def boom():
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store, "ping", boom)
    assert service.health()["store"] == "error"
