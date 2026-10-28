"""Uwierzytelnianie tokenem: REST, /ws, symulator i ciało w C (docs/spec.md, sekcje 1 i 10)."""

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from host.api.auth import AuthConfigError, bearer_ok, token_from_env, ws_token_ok
from host.api.main import create_app
from host.core.clock import ManualClock
from host.core.config import Config
from host.store import Store
from host.tests.servers import free_port, start, stop, until
from host.tests.test_api_service import hb
from sim.body import SimRunner
from sim.model import SimConfig

TOKEN = "s3kretny_Token-1234567890"
FIRMWARE = Path(__file__).parents[2] / "firmware"


def test_token_from_env(monkeypatch):
    monkeypatch.delenv("IRIS_TOKEN", raising=False)
    assert token_from_env() is None
    monkeypatch.setenv("IRIS_TOKEN", "")
    assert token_from_env() is None
    monkeypatch.setenv("IRIS_TOKEN", TOKEN)
    assert token_from_env() == TOKEN
    for bad in ("krotki", "a" * 129, "ma spacje w srodku!!", "zażółć-gęślą-jaźń-123"):
        monkeypatch.setenv("IRIS_TOKEN", bad)
        with pytest.raises(AuthConfigError) as e:
            token_from_env()
        assert bad not in str(e.value)  # sekret nie trafia do komunikatu
    monkeypatch.setenv("IRIS_TOKEN", "krotki")
    with pytest.raises(AuthConfigError):
        create_app()  # host nie startuje z niepoprawnym tokenem


def test_header_and_subprotocol_checks():
    assert bearer_ok(f"Bearer {TOKEN}", TOKEN) and bearer_ok(f"bearer  {TOKEN} ", TOKEN)
    for bad in (None, "", TOKEN, f"Basic {TOKEN}", f"Bearer {TOKEN}x", "Bearer "):
        assert not bearer_ok(bad, TOKEN)
    assert ws_token_ok(["iris", f"token.{TOKEN}"], TOKEN)
    assert not ws_token_ok(["iris"], TOKEN) and not ws_token_ok([f"token.{TOKEN[:-1]}"], TOKEN)
    assert not ws_token_ok([TOKEN], TOKEN)


@pytest.fixture
def client():
    clock = ManualClock()
    app = create_app(Config(), clock=clock, store=Store(clock=clock), run_tick_loop=False, token=TOKEN)
    with TestClient(app) as c:
        yield c


def test_rest_requires_the_bearer_token(client):
    for path in ("/health", "/self", "/devices", "/events", "/facts"):
        r = client.get(path)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer", path
        assert client.get(path, headers={"Authorization": "Bearer zly-token-1234567890"}).status_code == 401
        assert client.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200, path
    assert client.post("/devices/hermes/activate").status_code == 401


def test_websocket_requires_the_token_in_a_subprotocol(client, caplog):
    with caplog.at_level(logging.DEBUG):
        for protocols in ([], ["iris"], ["iris", "token.zly-token-1234567890"]):
            with client.websocket_connect("/ws?device=laptop", subprotocols=protocols) as ws:
                with pytest.raises(WebSocketDisconnect) as e:
                    ws.receive_text()
                assert e.value.code == 4003
        with client.websocket_connect("/ws?device=laptop", subprotocols=["iris", f"token.{TOKEN}"]) as ws:
            assert ws.accepted_subprotocol == "iris"
            ws.send_text(hb(50))
            r = client.get("/devices", headers={"Authorization": f"Bearer {TOKEN}"})
            assert r.json()["active"] == "laptop"
    assert TOKEN not in caplog.text and "zly-token" not in caplog.text
    r = client.get("/events", headers={"Authorization": f"Bearer {TOKEN}"})
    assert TOKEN not in r.text


def test_without_a_token_the_iris_subprotocol_is_still_selected():
    clock = ManualClock()
    app = create_app(Config(), clock=clock, store=Store(clock=clock), run_tick_loop=False, token=None)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        with c.websocket_connect("/ws", subprotocols=["iris"]) as ws:
            assert ws.accepted_subprotocol == "iris"
        with c.websocket_connect("/ws") as ws:
            assert ws.accepted_subprotocol is None


def test_without_a_token_requests_through_a_proxy_are_refused(caplog):
    clock = ManualClock()
    app = create_app(Config(), clock=clock, store=Store(clock=clock), run_tick_loop=False, token=None)
    with TestClient(app) as c, caplog.at_level(logging.WARNING):
        for header in ("Forwarded", "X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP", "True-Client-IP"):
            proxied = {header: "203.0.113.7"}
            assert c.get("/health", headers=proxied).status_code == 401, header
            with c.websocket_connect("/ws?device=telefon", subprotocols=["iris"], headers=proxied) as ws:
                with pytest.raises(WebSocketDisconnect) as e:
                    ws.receive_text()
                assert e.value.code == 4003, header
            assert c.get("/app/", headers=proxied).status_code == 200  # pliki klienta zostają publiczne
        assert c.get("/health").status_code == 200  # bezpośrednio, bez nagłówków proxy
        assert c.get("/devices").json()["devices"] == []  # żadne połączenie przez proxy nie zostało przyjęte
    assert "przez proxy lub tunel" in caplog.text


def test_with_a_token_proxied_requests_need_only_the_token(client):
    proxied = {"X-Forwarded-For": "203.0.113.7", "Authorization": f"Bearer {TOKEN}"}
    assert client.get("/health", headers=proxied).status_code == 200
    with client.websocket_connect("/ws?device=telefon", subprotocols=["iris", f"token.{TOKEN}"],
                                  headers={"CF-Connecting-IP": "203.0.113.7"}) as ws:
        assert ws.accepted_subprotocol == "iris"


def test_simulator_and_c_body_connect_only_with_the_token(tmp_path, monkeypatch):
    port = free_port()
    cfg = Config.model_validate({"store": {"path": ":memory:"}, "api": {"tick_interval_s": 0.1}})
    server = start(create_app(cfg, token=TOKEN), port)
    url = f"ws://127.0.0.1:{port}/ws"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        async def sims():
            good = SimRunner(SimConfig(heartbeat_interval_s=0.1), url, say=lambda s: None, token=TOKEN)
            bad = SimRunner(SimConfig(heartbeat_interval_s=0.1), url + "?device=intruz", say=lambda s: None,
                            token="zly-token-1234567890")
            tasks = [asyncio.create_task(good.run()), asyncio.create_task(bad.run())]
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers) as http:
                async def ok():
                    return (await http.get("/health")).json()["link"]["state"] == "ok"

                await until(ok)
                await asyncio.sleep(1.0)  # zły token: kolejne próby kończą się 4003
                ids = [d["id"] for d in (await http.get("/devices")).json()["devices"]]
            assert ids == ["hermes"] and bad.connections == 0
            good.stop()
            bad.stop()
            await asyncio.gather(*tasks)

        asyncio.run(sims())

        if shutil.which("cc") and shutil.which("make"):
            build = tmp_path / "fw"
            subprocess.run(["make", "-C", str(FIRMWARE), "posix", f"BUILD={build}"], check=True, capture_output=True)
            for token, expect in ((TOKEN, "link up"), ("zly-token-1234567890", "link down 4003")):
                out = _run_hermes(build / "hermes", url, token)
                assert expect in out, out
    finally:
        stop(*server)


def _run_hermes(exe, url, token):
    """Uruchamia port POSIX na chwilę i zwraca jego stdout."""
    proc = subprocess.Popen(
        [str(exe), "--url", url, "--device", "pi"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={"IRIS_TOKEN": token, "PATH": "/usr/bin:/bin"},
    )
    try:
        out, err = proc.communicate("", timeout=1.5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        out, err = proc.communicate(timeout=5)
    assert "token: tak" in err
    return out
