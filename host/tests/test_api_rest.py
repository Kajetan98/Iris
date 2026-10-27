import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from host.api.main import create_app
from host.core.clock import ManualClock
from host.core.config import Config
from host.store import Store


def frame(type_, body, t="event", id_="1", ts=500):
    return json.dumps({"v": 1, "t": t, "id": id_, "ts": ts, "type": type_, "body": body})


def hb(batt=71):
    return frame("body.heartbeat", {"batt_pct": batt, "link_rssi": -58})


def eventually(fn, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("warunek nie został spełniony w czasie")


@pytest.fixture
def clock():
    return ManualClock()


@pytest.fixture
def client(clock):
    cfg = Config.model_validate({"api": {"ack_timeout_s": 0.3}})
    app = create_app(cfg, clock, Store(clock=clock), run_tick_loop=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def service(client):
    return client.app.state.service


def connected(client):
    return client.get("/health").json()["link"]["connected"]


# --- stan ---


def test_get_self(client):
    r = client.get("/self")
    assert r.status_code == 200
    body = r.json()
    assert body["self"]["name"] == "IRIS" and body["self"]["mode"] == "nominal"
    assert body["self"]["continuity"] == {"sessions": 1, "known_facts": 0}
    assert body["body"]["link"] == "unknown"


def test_health_before_any_contact(client):
    h = client.get("/health").json()
    assert h["status"] == "degraded" and h["store"] == "ok"
    assert h["link"] == {"state": "unknown", "connected": False, "rssi": None, "last_seen_ms": None}


def test_self_history_defaults_to_current_session(client, service):
    r = client.get("/self/history")
    assert r.status_code == 200
    data = r.json()
    assert [e["type"] for e in data["events"]] == ["host.start"]
    assert len(data["snapshots"]) == 1
    assert data["snapshots"][0]["self"]["name"] == "IRIS"
    assert data["since"].startswith("2026-01-01T12:00:00")


def test_self_history_filters_to_state_events_and_since(client, service, clock):
    service.handle_text(frame("button", {"id": "b", "kind": "press"}))
    clock.advance(2)  # krócej niż link_timeout_s, więc bez link.lost
    service.handle_text(frame("body.fault", {"sensor": "gps", "code": "no_fix"}))
    r = client.get("/self/history", params={"since": "2026-01-01T12:00:01Z"})
    types = [e["type"] for e in r.json()["events"]]
    assert types == ["body.fault"]  # button nie jest zmianą stanu, host.start jest wcześniejszy
    all_types = [e["type"] for e in client.get("/self/history").json()["events"]]
    assert all_types == ["host.start", "device.switch", "body.fault"]  # pierwsze urządzenie


# --- zdarzenia ---


def test_events_filters_and_paging(client, service, clock):
    for i in range(3):
        clock.advance(1)
        service.handle_text(frame("button", {"id": f"b{i}", "kind": "press"}))
    service.handle_text(frame("body.fault", {"sensor": "imu", "code": 1}))

    all_ev = client.get("/events").json()["events"]
    assert [e["type"] for e in all_ev] == [
        "host.start", "device.switch", "button", "button", "button", "body.fault",
    ]
    assert all_ev[2]["source"] == "body" and all_ev[2]["ts_ms"] == 500
    assert all_ev[2]["data"] == {"id": "b0", "kind": "press"}

    r = client.get("/events", params=[("type", "button"), ("type", "body.fault")])
    assert len(r.json()["events"]) == 4
    r = client.get("/events", params={"type": "button", "limit": 2})
    page = r.json()["events"]
    assert [e["data"]["id"] for e in page] == ["b0", "b1"]
    r = client.get("/events", params={"type": "button", "after_id": page[-1]["id"]})
    assert [e["data"]["id"] for e in r.json()["events"]] == ["b2"]
    r = client.get("/events", params={"since": "2026-01-01T12:00:02Z", "type": "button"})
    assert [e["data"]["id"] for e in r.json()["events"]] == ["b1", "b2"]


@pytest.mark.parametrize(
    "params",
    [{"since": "2026-01-01T12:00:00"}, {"since": "wczoraj"}, {"limit": 0}, {"limit": 5000}, {"after_id": -1}],
)
def test_events_rejects_bad_params(client, params):
    assert client.get("/events", params=params).status_code == 422


def test_naive_since_is_rejected_in_history_too(client):
    assert client.get("/self/history", params={"since": "2026-01-01T12:00:00"}).status_code == 422


# --- fakty ---


def test_facts_list_patch_delete(client, service):
    a = service.store.add_fact("Mieszkam w Gdańsku", "stated")
    b = service.store.add_fact("Lubi kawę", "inferred", 0.4)

    facts = client.get("/facts").json()["facts"]
    assert [f["text"] for f in facts] == ["Mieszkam w Gdańsku", "Lubi kawę"]
    assert set(facts[0]) == {"id", "text", "source", "confidence", "created_at", "updated_at"}
    assert [f["id"] for f in client.get("/facts", params={"source": "inferred"}).json()["facts"]] == [b.id]
    assert client.get("/facts", params={"source": "guessed"}).status_code == 422

    r = client.patch(f"/facts/{b.id}", json={"text": "Pije herbatę"})
    assert r.status_code == 200
    assert (r.json()["text"], r.json()["source"], r.json()["confidence"]) == ("Pije herbatę", "stated", 1.0)
    r = client.patch(f"/facts/{a.id}", json={"confidence": 0.7})
    assert r.json()["confidence"] == 0.7 and r.json()["source"] == "stated"

    assert client.delete(f"/facts/{a.id}").status_code == 204
    assert client.delete(f"/facts/{a.id}").status_code == 404
    assert [f["id"] for f in client.get("/facts").json()["facts"]] == [b.id]


@pytest.mark.parametrize(
    "payload",
    [{}, {"text": None}, {"text": "  "}, {"confidence": 1.5}, {"source": "guessed"}, {"color": "red"}],
)
def test_patch_fact_validation(client, service, payload):
    f = service.store.add_fact("a", "stated")
    assert client.patch(f"/facts/{f.id}", json=payload).status_code == 422
    assert service.store.get_fact(f.id).text == "a"


def test_patch_missing_fact_is_404(client):
    assert client.patch("/facts/99", json={"text": "x"}).status_code == 404


def test_delete_updates_known_facts_in_self(client, service):
    f = service.store.add_fact("a", "stated")
    service.refresh_known_facts()
    assert client.get("/self").json()["self"]["continuity"]["known_facts"] == 1
    client.delete(f"/facts/{f.id}")
    assert client.get("/self").json()["self"]["continuity"]["known_facts"] == 0


# --- WebSocket ---


def test_websocket_frames_reach_the_state(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text(hb(64))
        eventually(lambda: client.get("/self").json()["body"]["batt_pct"] == 64)
        assert client.get("/health").json()["link"]["connected"] is True
        assert client.get("/health").json()["status"] == "ok"
    eventually(lambda: not connected(client))


def test_bad_frames_do_not_close_the_connection(client, service):
    with client.websocket_connect("/ws") as ws:
        ws.send_text("{nie json")
        ws.send_text(frame("teleport", {}))
        ws.send_bytes(b"\x07")
        ws.send_text(hb(55))
        eventually(lambda: client.get("/self").json()["body"]["batt_pct"] == 55)
        assert service.rejected_frames == 3


def test_second_connection_replaces_the_first(client):
    with client.websocket_connect("/ws") as first:
        eventually(lambda: connected(client))
        with client.websocket_connect("/ws") as second:
            with pytest.raises(WebSocketDisconnect) as e:
                first.receive_text()
            assert e.value.code == 4000
            second.send_text(hb(42))
            eventually(lambda: client.get("/self").json()["body"]["batt_pct"] == 42)
            assert connected(client)


# --- akcje ---


def test_action_without_body_is_409(client):
    r = client.post("/actions", json={"type": "ping"})
    assert r.status_code == 409


def test_action_validation_is_422(client):
    with client.websocket_connect("/ws"):
        eventually(lambda: connected(client))
        assert client.post("/actions", json={"type": "self.destruct"}).status_code == 422
        assert client.post("/actions", json={"type": "haptic", "body": {"pattern": "loud"}}).status_code == 422
        assert client.post("/actions", json={}).status_code == 422


def test_action_with_ack(client):
    with client.websocket_connect("/ws") as ws:
        eventually(lambda: connected(client))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                client.post, "/actions", json={"type": "haptic", "body": {"pattern": "attention"}}
            )
            sent = json.loads(ws.receive_text())
            assert (sent["t"], sent["type"], sent["body"]) == ("action", "haptic", {"pattern": "attention"})
            ws.send_text(frame("haptic", {"ref": sent["id"], "ok": True}, t="ack"))
            r = pending.result(timeout=5)
    assert r.status_code == 200
    assert r.json() == {"id": sent["id"], "device": "hermes", "ack": {"ref": sent["id"], "ok": True, "err": None}}
    types = [e["type"] for e in client.get("/events", params=[("type", "action.sent"), ("type", "action.ack")]).json()["events"]]
    assert types == ["action.sent", "action.ack"]


def test_action_ack_error_is_passed_through(client):
    with client.websocket_connect("/ws") as ws:
        eventually(lambda: connected(client))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, "/actions", json={"type": "gpio.set", "body": {"pin": 99, "value": 1}})
            sent = json.loads(ws.receive_text())
            ws.send_text(frame("gpio.set", {"ref": sent["id"], "ok": False, "err": "pin poza listą"}, t="ack"))
            r = pending.result(timeout=5)
    assert r.status_code == 200
    assert r.json()["ack"] == {"ref": sent["id"], "ok": False, "err": "pin poza listą"}


def test_action_without_wait_ack(client):
    with client.websocket_connect("/ws") as ws:
        eventually(lambda: connected(client))
        r = client.post("/actions", json={"type": "ping", "wait_ack": False})
        assert r.status_code == 200 and r.json()["ack"] is None
        assert json.loads(ws.receive_text())["type"] == "ping"


def test_action_ack_timeout_is_504(client):
    with client.websocket_connect("/ws") as ws:
        eventually(lambda: connected(client))
        r = client.post("/actions", json={"type": "ping"})
        assert r.status_code == 504
        assert json.loads(ws.receive_text())["type"] == "ping"  # akcja została wysłana


# --- cykl życia ---


def test_shutdown_closes_the_session(clock):
    store = Store(clock=clock)
    app = create_app(Config(), clock, store, run_tick_loop=False)
    with TestClient(app):
        pass
    assert store.sessions()[0].ended_at is not None
    assert [e.type for e in store.events()][-1] == "host.stop"


def test_module_level_app_is_importable():
    from host.api.main import app

    assert app.title == "IRIS host"


def test_oversized_action_is_422(client):
    with client.websocket_connect("/ws") as ws:
        eventually(lambda: connected(client))
        r = client.post("/actions", json={"type": "config.set", "body": {"key": "volume", "value": "x" * 600}})
        assert r.status_code == 422 and "512" in r.json()["detail"]
