"""Klient w przeglądarce (host/api/static) w Chromium z Playwright, przeciw prawdziwemu hostowi.

Scenariusze są w host/tests/browser/drive.js; pytest uruchamia host, scenariusz i sprawdza obie strony.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
from websockets.sync.client import connect as ws_connect

from host.api.main import create_app
from host.brain.audio import STT_RATE
from host.core.config import Config
from host.tests.audio_helpers import dominant_hz
from host.tests.servers import free_port, ollama_stub_app, start, stop
from host.tests.test_brain_voice import TTS_HZ, FakeSTT, FakeTTS
from host.tests.test_firmware import CASES, INVALID, raw_action, sim_ack

DRIVE = Path(__file__).parent / "browser" / "drive.js"
NODE = shutil.which("node")
TOKEN = "przegladarka_Token-123456"


def _playwright_available():
    if not NODE:
        return False
    return subprocess.run([NODE, "-e", "require('playwright')"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _playwright_available(), reason="brak node lub playwright")


def browser(scenario, base, timeout=90, expected_errors=(), **opts):
    r = subprocess.run([NODE, str(DRIVE), scenario, base, json.dumps(opts)], capture_output=True, text=True,
                       timeout=timeout)
    lines = r.stdout.strip().splitlines()
    assert lines, r.stderr
    out = json.loads(lines[-1])
    assert out["ok"], out
    assert out["errors"] == list(expected_errors), out["errors"]  # bez innych błędów JS na stronie
    return out["value"]


def poll(fn, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("warunek nie został spełniony w czasie")


@pytest.fixture
def voice_host():
    ollama_app, _ = ollama_stub_app("Mam się dobrze. Dziękuję za pytanie.")
    ollama_port = free_port()
    ollama = start(ollama_app, ollama_port)
    cfg = Config.model_validate({
        "store": {"path": ":memory:"},
        "api": {"tick_interval_s": 0.1, "ack_timeout_s": 3.0},
        "brain": {"llm": {"engine": "ollama", "model": "m", "retries": 0, "timeout_s": 5,
                          "base_url": f"http://127.0.0.1:{ollama_port}"}},
    })
    stt, tts = FakeSTT("Jak się masz"), FakeTTS(0.6)
    port = free_port()
    server = start(create_app(cfg, stt=stt, tts=tts, token=None), port)
    yield f"http://127.0.0.1:{port}", stt
    stop(*server)
    stop(*ollama)


class OtherDevice:
    """Drugie urządzenie (zwykły klient WebSocket w wątku): caps, heartbeat, zapis akcji."""

    def __init__(self, url, token=None):
        protocols = ["iris", f"token.{token}"] if token else None
        self._cm = ws_connect(url, subprotocols=protocols)
        self.ws = self._cm.__enter__()
        self.received = []
        self._stop = threading.Event()
        self.ws.send(json.dumps({"v": 1, "t": "event", "id": "o.1", "ts": 0, "type": "device.caps",
                                 "body": {"has": ["mic", "speaker"]}}))
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        n = 1
        while not self._stop.is_set():
            n += 1
            try:
                self.ws.send(json.dumps({"v": 1, "t": "event", "id": f"o.{n}", "ts": n, "type": "body.heartbeat",
                                         "body": {"batt_pct": 80, "link_rssi": -50}}))
                while True:
                    self.received.append(json.loads(self.ws.recv(timeout=0.3)))
            except TimeoutError:
                continue
            except Exception:
                return

    def close(self):
        self._stop.set()
        self._cm.__exit__(None, None, None)


def test_browser_acks_follow_the_same_rules_as_the_body():
    port = free_port()
    server = start(create_app(Config.model_validate({"store": {"path": ":memory:"}}), token=None), port)
    try:
        frames = [raw_action(t, b) for t, b in CASES] + INVALID
        results = browser("acks", f"http://127.0.0.1:{port}", frames=frames)
    finally:
        stop(*server)
    for (type_, body), got in zip(CASES, results):
        py_ack, _ = sim_ack(raw_action(type_, body))
        expected = (py_ack.body.ok, py_ack.body.err)
        # Różnice z założenia: brak GPIO, klipów i (w Chromium bez dotyku) wibracji.
        # JSON.parse nie odróżnia 5.0 od 5, więc pin 5.0 to w przeglądarce liczba całkowita.
        float_pin = isinstance(body.get("pin"), float) and float(body["pin"]).is_integer()
        if type_ == "gpio.set" and (py_ack.body.err != "bad_body" or float_pin):
            expected = (False, "unsupported")
        elif type_ == "speak.clip" and py_ack.body.err != "bad_body":
            expected = (False, "unknown_clip")
        elif type_ == "haptic" and py_ack.body.ok:
            expected = (False, "unsupported")
        assert (got["ack"]["ok"], got["ack"].get("err")) == expected, (type_, body)
    for frame, got in zip(INVALID, results[len(CASES):]):
        assert got == {"ack": None, "frameErrors": 1}, frame


def test_voice_turn_through_the_browser(voice_host):
    base, stt = voice_host
    out = browser("voice", base, device="przegladarka", token="", hz=440, seconds=1.5)
    pcm, rate = stt.calls[0]
    assert rate == STT_RATE and abs(len(pcm) / 2 / rate - 1.5) < 0.06
    assert abs(dominant_hz(pcm) - 440) < 5  # host dostał to, co zakodowała przeglądarka
    (played,) = out["played"]
    assert played["audioId"] == 1 and abs(played["duration"] - 1.2) < 0.1  # dwa zdania po 0,6 s
    samples = np.array(out["samples"], dtype=np.int16)
    assert abs(dominant_hz(samples, rate=out["rate"]) - TTS_HZ) < 5  # przeglądarka zdekodowała odpowiedź
    assert out["caps"]["has"][:2] == ["mic", "speaker"] and out["caps"]["clips"] == []
    with httpx.Client(base_url=base) as http:
        (dev,) = http.get("/devices").json()["devices"]
        assert dev["id"] == "przegladarka" and dev["active"] and dev["rssi"] is None
        assert dev["caps"]["has"][:2] == ["mic", "speaker"]
        acks = http.get("/events", params={"type": "action.ack"}).json()["events"]
        assert [e["data"] for e in acks] == [{"ref": "h1", "ok": True, "err": None}]


def test_real_microphone_of_the_browser(voice_host):
    base, stt = voice_host
    browser("mic", base, device="laptop", token="", ms=1200)
    poll(lambda: stt.calls)
    pcm, rate = stt.calls[0]
    seconds = len(pcm) / 2 / rate
    assert 0.9 < seconds < 1.8, seconds  # tyle, ile przycisk był wciśnięty
    assert np.abs(np.frombuffer(pcm, np.int16)).max() > 100  # sztuczny mikrofon Chromium daje dźwięk


def test_inactive_browser_can_take_over(voice_host):
    base, _ = voice_host
    other = OtherDevice(base.replace("http", "ws") + "/ws?device=hermes")
    try:
        with httpx.Client(base_url=base) as http:
            poll(lambda: http.get("/devices").json()["active"] == "hermes")
            out = browser("switching", base, device="telefon", token="")
            assert out == {"talkDisabled": True, "before": False, "after": True, "activateHidden": True}
            assert http.get("/devices").json()["active"] == "telefon"
            switch = http.get("/events", params={"type": "device.switch"}).json()["events"][-1]
            assert switch["data"] == {"from": "hermes", "to": "telefon", "by": "device"}
        poll(lambda: any(m.get("type") == "device.active" and m["body"] == {"active": False}
                         for m in other.received))
    finally:
        other.close()


def test_token_in_the_browser():
    port = free_port()
    server = start(create_app(Config.model_validate({"store": {"path": ":memory:"}}), token=TOKEN), port)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base) as http:
            assert http.get("/app/").status_code == 200  # pliki klienta bez tokenu
            assert http.get("/devices").status_code == 401
            out = browser("badToken", base, device="laptop", token="zly-token-1234567890")
            assert "brak lub zły token" in out["status"]
            out = browser("connectOnly", base, device="laptop", token=TOKEN)
            assert out["status"] == "Połączono jako laptop"
            devices = http.get("/devices", headers={"Authorization": f"Bearer {TOKEN}"}).json()["devices"]
            assert [d["id"] for d in devices] == ["laptop"]
    finally:
        stop(*server)


def test_installable_app():
    port = free_port()
    server = start(create_app(Config.model_validate({"store": {"path": ":memory:"}}), token=None), port)
    try:
        out = browser("pwa", f"http://127.0.0.1:{port}")
    finally:
        stop(*server)
    m = out["manifest"]
    assert (m["name"], m["start_url"], m["display"]) == ("IRIS", "/app/", "standalone")
    assert {i["sizes"] for i in m["icons"]} >= {"192x192", "512x512"}
    assert {"/app/", "/app/app.js", "/app/protocol.js", "/app/icon-512.png"} <= set(out["cached"])
    assert out["secure"]


def test_host_on_plain_and_tls_ports_shares_one_state(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("brak openssl")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-subj", "/CN=iris", "-addext", "subjectAltName=IP:127.0.0.1"],
                   check=True, capture_output=True)
    config = tmp_path / "iris.toml"
    config.write_text('[store]\npath = ":memory:"\n')
    plain, tls = free_port(), free_port()
    env = {**os.environ, "IRIS_CONFIG": str(config), "IRIS_TOKEN": TOKEN}
    proc = subprocess.Popen(
        [sys.executable, "-m", "host.api.serve", "--port", str(plain), "--tls-port", str(tls),
         "--certfile", str(cert), "--keyfile", str(key), "--log-level", "warning"],
        env=env, cwd=Path(__file__).parents[2], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        def up():
            try:
                return httpx.get(f"https://127.0.0.1:{tls}/health", headers=headers, verify=False).status_code == 200
            except httpx.TransportError:
                return False

        poll(up, timeout=15)
        hermes = OtherDevice(f"ws://127.0.0.1:{plain}/ws?device=hermes", token=TOKEN)
        try:
            # Z samopodpisanym certyfikatem Chrome nie rejestruje service workera: klient działa,
            # ale nie da się go zainstalować (spec, sekcja 1, "Klient w przeglądarce").
            out = browser("connectOnly", f"https://127.0.0.1:{tls}", device="telefon", token=TOKEN, stayMs=300,
                          expected_errors=["An SSL certificate error occurred when fetching the script."])
            assert out["status"] == "Połączono jako telefon" and out["active"] == "W spoczynku"
            body = httpx.get(f"http://127.0.0.1:{plain}/devices", headers=headers).json()
            assert body["active"] == "hermes" and [d["id"] for d in body["devices"]] == ["hermes", "telefon"]
        finally:
            hermes.close()
    finally:
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=15)
    assert proc.returncode == 0, err
