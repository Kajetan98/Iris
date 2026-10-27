"""Port POSIX firmware (firmware/port/posix) z prawdziwym hostem i serwerem websockets.

build/hermes to rdzeń C i klient WebSocket (firmware/net) na gnieździe TCP. Testy uruchamiają
go jako proces, wysyłają komendy na stdin i czytają wywołania sprzętu ze stdout.
"""

import asyncio
import json
import re
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

from host.api.main import create_app
from host.brain.audio import STT_RATE, OpusDecoder, resample
from host.core.config import Config
from host.tests.audio_helpers import dominant_hz, opus_packets
from host.tests.servers import free_port, ollama_stub_app, start, stop
from host.tests.test_brain_voice import TTS_HZ, FakeSTT, FakeTTS

FIRMWARE = Path(__file__).parents[2] / "firmware"


@pytest.fixture(scope="session")
def hermes(tmp_path_factory):
    if not (shutil.which("cc") and shutil.which("make")):
        pytest.skip("brak kompilatora C lub make")
    build = tmp_path_factory.mktemp("hermes")
    subprocess.run(["make", "-C", str(FIRMWARE), "posix", f"BUILD={build}"], check=True, capture_output=True)
    return build / "hermes"


class Body:
    """Proces hermes: komendy na stdin, linie stdout i stderr zbierane w wątkach."""

    def __init__(self, exe, url, *args):
        self.proc = subprocess.Popen(
            [str(exe), "--url", url, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self.out, self.err = [], []
        self._cond = threading.Condition()
        for stream, sink in ((self.proc.stdout, self.out), (self.proc.stderr, self.err)):
            threading.Thread(target=self._read, args=(stream, sink), daemon=True).start()

    def _read(self, stream, sink):
        for line in stream:
            with self._cond:
                sink.append((time.monotonic(), line.rstrip("\n")))
                self._cond.notify_all()

    def send(self, command):
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def wait(self, pattern, *, after=0, timeout=8.0, stream=None):
        """Pierwsza linia od indeksu after pasująca do wyrażenia; zwraca (indeks, czas, linia)."""
        sink = self.out if stream is None else stream
        rx = re.compile(pattern)
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                for i in range(after, len(sink)):
                    if rx.search(sink[i][1]):
                        return i, sink[i][0], sink[i][1]
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"brak linii {pattern!r}; stdout={self.out[-15:]} stderr={self.err[-15:]}")
                self._cond.wait(left)

    def lines(self, pattern):
        with self._cond:
            return [line for _, line in self.out if re.search(pattern, line)]

    def status(self):
        mark = len(self.out)
        self.send("status")
        _, _, line = self.wait(r"^status ", after=mark)
        return dict(kv.split("=") for kv in line.split()[1:])

    def quit(self):
        self.send("quit")
        return self.proc.wait(5)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)


@pytest.fixture
def bodies():
    started = []
    yield lambda *a: started.append(Body(*a)) or started[-1]
    for b in started:
        b.close()


def host_config(**extra):
    cfg = {
        "store": {"path": ":memory:"},
        "link": {"timeout_s": 1.5, "recover_s": 0.4},  # rdzeń C wysyła heartbeat co 1 s
        "api": {"tick_interval_s": 0.1, "ack_timeout_s": 3.0},
        "drives": {"battery_low": {"direction": "below", "enter": 20, "exit": 25,
                                   "enter_hold_s": 0.3, "exit_hold_s": 0.3}},
    }
    cfg.update(extra)
    return Config.model_validate(cfg)


def poll(fn, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("warunek nie został spełniony w czasie")


def events(http, *types):
    return http.get("/events", params=[("limit", 1000), *[("type", t) for t in types]]).json()["events"]


def test_body_on_real_host_events_actions_and_clean_close(hermes, bodies):
    port = free_port()
    server = start(create_app(host_config()), port)
    try:
        body = bodies(hermes, f"ws://127.0.0.1:{port}/ws", "--gpio-pins", "5,6")
        body.wait(r"^link up$")
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
            poll(lambda: http.get("/health").json()["link"]["state"] == "ok")
            health = http.get("/health").json()
            assert health["status"] == "ok" and health["link"]["connected"] and health["link"]["rssi"] == -58

            (boot,) = events(http, "system.boot")
            assert re.fullmatch(r"[0-9a-f]{8}", boot["boot_id"]) and boot["source"] == "body"
            assert boot["data"] == {"boot_id": boot["boot_id"], "fw": "hermes-0.1.0", "reset_reason": "power"}

            # akcje z hosta: ack i wywołania sprzętu
            def act(type_, body_=None):
                r = http.post("/actions", json={"type": type_, "body": body_ or {}})
                assert r.status_code == 200, r.text
                return r.json()["ack"]

            assert act("ping")["ok"]
            assert act("haptic", {"pattern": "warn"})["ok"]
            assert act("gpio.set", {"pin": 5, "value": 1})["ok"]
            assert act("gpio.set", {"pin": 7, "value": 1}) == {"ref": "h4", "ok": False, "err": "pin_not_allowed"}
            assert act("speak.clip", {"clip": "ready"})["ok"]
            assert act("speak.clip", {"clip": "fanfare"})["err"] == "unknown_clip"
            assert act("config.set", {"key": "volume", "value": 40})["ok"]
            assert act("speak", {"audio_id": 9})["err"] == "no_audio"
            body.wait(r"^config volume 40000$")
            assert body.lines(r"^(haptic|gpio|clip|config) ") == [
                "haptic warn", "gpio 5 1", "clip ready", "config volume 40000",
            ]

            # komendy ciała: zdarzenia u hosta, z polskimi znakami w JSON
            for cmd in ("press long", "local światło w kuchni", "presence on", "gps 52.2297 21.0122",
                        "fault gps nofix", "clear gps", "batt 15"):
                body.send(cmd)
            (enter,) = poll(lambda: events(http, "drive.enter"))
            assert enter["data"]["drive"] == "battery_low" and enter["data"]["value"] == 15
            by_type = {e["type"]: e["data"] for e in events(http)}
            assert by_type["button"] == {"id": "b1", "kind": "long"}
            assert by_type["local.command"] == {"cmd": "światło w kuchni", "result": "ok"}
            assert by_type["body.fault"] == {"sensor": "gps", "code": "nofix"}
            assert by_type["body.fault.clear"] == {"sensor": "gps"}
            sensors = [e["data"] for e in events(http, "body.sensor")]
            assert any(s["presence"] is True for s in sensors)
            gps = [s["gps"] for s in sensors if s["gps"]]
            assert gps == [{"lat": pytest.approx(52.2297, abs=1e-6), "lon": pytest.approx(21.0122, abs=1e-6)}]
            assert http.get("/self").json()["body"]["batt_pct"] == 15

            # czyste zamknięcie: ramka close 1000, host widzi rozłączenie
            assert body.quit() == 0
            assert body.lines(r"^link down") == ["link down 1000 -"]
            poll(lambda: not http.get("/health").json()["link"]["connected"])
    finally:
        stop(*server)


def test_reconnects_after_replacement_drop_and_host_restart(hermes, bodies):
    port = free_port()
    url = f"ws://127.0.0.1:{port}/ws"
    server = start(create_app(host_config()), port)
    body = bodies(hermes, url)
    try:
        body.wait(r"^link up$")

        # 1. inne połączenie zastępuje ciało: kod 4000, ponowienie po 0,5 s
        with ws_connect(url) as other:
            i, t_down, _ = body.wait(r"^link down 4000 -$")
            other.close()
        _, t_up, _ = body.wait(r"^link up$", after=i)
        assert 0.45 <= t_up - t_down < 1.5

        # 2. utrata zasięgu: host oznacza link_lost, po powrocie link.restored
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
            poll(lambda: http.get("/health").json()["link"]["state"] == "ok")
            mark = len(body.out)
            body.send("drop 2.5")
            body.wait(r"^link down 1006 transport$", after=mark)
            poll(lambda: events(http, "link.lost"))
            body.wait(r"^link up$", after=mark, timeout=5)
            poll(lambda: events(http, "link.restored"))

        # 3. restart hosta: zdarzenia czekają w buforze ciała i trafiają do nowego hosta po kolei
        mark = len(body.out)
        stop(*server)
        body.wait(r"^link down 1012 -$", after=mark)  # uvicorn przy zamykaniu: 1012 (restart usługi)
        err_mark = len(body.err)
        body.send("press")
        body.send("local lampa")
        assert body.status()["outbox"] == "2"
        body.wait(r"ponawiam za 2000 ms", after=err_mark, stream=body.err)
        delays = [int(m) for m in re.findall(r"ponawiam za (\d+) ms", "\n".join(line for _, line in body.err[err_mark:]))]
        assert delays[:2] == [1000, 2000]  # 0,5 s po zerwaniu, potem podwajanie

        server = start(create_app(host_config()), port)
        body.wait(r"^link up$", after=mark, timeout=8)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
            got = poll(lambda: [e for e in events(http) if e["source"] == "body" and e["type"] != "body.sensor"][:3])
            assert [e["type"] for e in got] == ["device.caps", "button", "local.command"]
            assert got[2]["data"]["cmd"] == "lampa"
            assert body.status()["outbox"] == "0"
            assert not events(http, "system.boot")  # ciało się nie restartowało
    finally:
        stop(*server)


def write_packets(path, packets):
    path.write_bytes(b"".join(struct.pack(">H", len(p)) + p for p in packets))


def read_packets(path):
    data, packets, pos = path.read_bytes(), [], 0
    while pos < len(data):
        (n,) = struct.unpack_from(">H", data, pos)
        packets.append(data[pos + 2:pos + 2 + n])
        pos += 2 + n
    return packets


def test_voice_turn_through_c_body(hermes, bodies, tmp_path):
    ollama_app, ollama = ollama_stub_app("Mam się dobrze. Dziękuję za pytanie.")
    ollama_port = free_port()
    ollama_server = start(ollama_app, ollama_port)
    cfg = host_config(brain={
        "llm": {"engine": "ollama", "model": "bielik", "retries": 0, "timeout_s": 3,
                "base_url": f"http://127.0.0.1:{ollama_port}"},
        "audio": {"error_clip": "error"},
    })
    stt, tts = FakeSTT("Jak się masz"), FakeTTS(0.6)
    port = free_port()
    server = start(create_app(cfg, stt=stt, tts=tts), port)
    try:
        body = bodies(hermes, f"ws://127.0.0.1:{port}/ws", "--audio-out", str(tmp_path))
        body.wait(r"^link up$")
        speech = tmp_path / "pytanie.bin"
        mic = opus_packets(440, 1.5)
        write_packets(speech, mic)
        body.send(f"sayopus {speech}")
        body.wait(rf"^utterance 1 {len(mic)}$")

        _, _, line = body.wait(r"^play ", timeout=10)
        audio_id, packets, present, nbytes, path = line.split()[1:]
        assert audio_id == "1" and packets == present
        pcm, rate = stt.calls[0]
        assert rate == STT_RATE and abs(len(pcm) / 2 / rate - 1.5) < 0.06
        assert abs(dominant_hz(pcm) - 440) < 5  # STT dostał to, co wysłał rdzeń C

        played = read_packets(Path(path))
        assert len(played) == int(packets) and sum(map(len, played)) == int(nbytes)
        dec = OpusDecoder()
        pcm_out = resample(np.concatenate([dec.decode(p) for p in played]), 48000, STT_RATE)
        assert abs(dominant_hz(pcm_out) - TTS_HZ) < 5
        assert abs(pcm_out.size / STT_RATE - 1.2) < 0.1  # dwa zdania po 0,6 s
        _, t_play, _ = body.wait(r"^play ")
        _, t_done, _ = body.wait(r"^played 1$", timeout=5)
        assert t_done - t_play >= int(packets) * 0.02 - 0.05  # odtwarzanie trwa tyle co nagranie

        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
            acks = events(http, "action.ack")
            assert [e["data"] for e in acks] == [{"ref": "h1", "ok": True, "err": None}]
        assert body.status()["busy"] == "0"
    finally:
        stop(*server)
        stop(*ollama_server)


class ScriptedServer:
    """Serwer websockets (nie FastAPI) w osobnym wątku; każde połączenie obsługuje kolejny skrypt."""

    def __init__(self, scripts):
        self.port = free_port()
        self.scripts = list(scripts)
        self.results = []
        self.errors = []
        self.done = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self._thread.start()
        assert self._ready.wait(5)

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with serve(self._handler, "127.0.0.1", self.port, ping_interval=None):
            self._ready.set()
            await self._stop.wait()

    async def _handler(self, ws):
        if not self.scripts:
            await ws.wait_closed()
            return
        script = self.scripts.pop(0)
        try:
            self.results.append(await asyncio.wait_for(script(ws), 10))
        except Exception as e:  # wynik sprawdza wątek testu
            self.errors.append(repr(e))
        if not self.scripts:
            self.done.set()

    def stop(self):
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)


def close_received(ws):
    """Kod z ramki close, którą odesłał lub wysłał klient C."""
    frame = ws.protocol.close_rcvd
    return frame.code if frame else None


async def recv_event(ws, type_):
    while True:
        msg = await ws.recv()
        if isinstance(msg, str):
            frame = json.loads(msg)
            if frame.get("type") == type_:
                return frame


def test_websocket_protocol_against_websockets_server(hermes, bodies):
    async def first(ws):
        boot = await recv_event(ws, "system.boot")
        # akcja w trzech fragmentach
        await ws.send(['{"v":1,"t":"action",', '"id":"x1","ts":0,', '"type":"ping","body":{}}'])
        ack = await recv_event(ws, "ping")
        # za duża wiadomość binarna: odrzucona, połączenie zostaje, licznik w heartbeat
        await ws.send(bytes(2000))
        await ws.send('{"v":1,"t":"action","id":"x2","ts":0,"type":"ping"}')
        ack2 = await recv_event(ws, "ping")
        heartbeat = await recv_event(ws, "body.heartbeat")
        latency = await ws.ping(b"abc")  # pong od klienta C
        await asyncio.wait_for(latency, 2)
        await ws.close(4000, "replaced")
        return {"boot": boot, "ack": ack, "ack2": ack2, "hb": heartbeat, "close": close_received(ws)}

    async def second(ws):
        await ws.send(b"\xff\xfe", text=True)  # tekst niepoprawny w UTF-8
        try:
            while True:
                await ws.recv()
        except ConnectionClosed:
            pass
        return {"close": close_received(ws)}

    async def third(ws):
        await recv_event(ws, "body.heartbeat")
        quit_now.set()
        try:
            while True:
                await ws.recv()
        except ConnectionClosed:
            pass
        return {"close": close_received(ws)}

    quit_now = threading.Event()
    server = ScriptedServer([first, second, third])
    try:
        body = bodies(hermes, f"ws://127.0.0.1:{server.port}/ws")
        assert quit_now.wait(15), (server.errors, body.out[-10:], body.err[-10:])
        assert body.quit() == 0
        assert server.done.wait(5) and not server.errors, server.errors
    finally:
        server.stop()

    r1, r2, r3 = server.results
    assert r1["boot"]["type"] == "system.boot"
    assert r1["ack"]["body"] == {"ref": "x1", "ok": True} and r1["ack2"]["body"]["ref"] == "x2"
    assert r1["hb"]["body"]["frame_errors"] == 1
    assert r1["close"] == 4000  # klient odesłał ten sam kod
    assert r2["close"] == 1007
    assert r3["close"] == 1000
    assert body.lines(r"^link down") == ["link down 4000 -", "link down 1007 utf8", "link down 1000 -"]


def test_two_c_bodies_switch_between_each_other(hermes, bodies, tmp_path):
    port = free_port()
    url = f"ws://127.0.0.1:{port}/ws"
    server = start(create_app(host_config()), port)
    try:
        first = bodies(hermes, url, "--device", "hermes")
        first.wait(r"^link up$")
        laptop = bodies(hermes, url, "--device", "laptop", "--batt", "15")
        laptop.wait(r"^link up$")
        laptop.wait(r"^active 0$")  # host: urządzenie w spoczynku
        laptop.wait(r"\?device=laptop, token: nie$", stream=laptop.err)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
            body = http.get("/devices").json()
            assert body["active"] == "hermes"
            assert [(d["id"], d["connected"]) for d in body["devices"]] == [("hermes", True), ("laptop", True)]
            time.sleep(0.6)  # dłużej niż enter_hold_s: bateria laptopa nie ma wpływu
            assert http.get("/self").json()["self"]["drives"] == []

            assert http.post("/devices/laptop/activate").status_code == 200
            laptop.wait(r"^active 1$")
            first.wait(r"^active 0$")
            (enter,) = poll(lambda: events(http, "drive.enter"))
            assert enter["device"] == "laptop" and enter["data"]["drive"] == "battery_low"
            speech = tmp_path / "x.bin"
            write_packets(speech, opus_packets(440, 0.2))
            mark = len(first.err)
            first.send(f"sayopus {speech}")
            first.wait(r"urządzenie nieaktywne", after=mark, stream=first.err)
            assert first.status()["active"] == "0"

            first.send("activate")
            first.wait(r"^active 1$", after=first.wait(r"^active 0$")[0])
            laptop.wait(r"^active 0$", after=laptop.wait(r"^active 1$")[0])
            exits = poll(lambda: events(http, "drive.exit"))
            assert [(e["device"], e["data"]) for e in exits] == [
                ("laptop", {"drive": "battery_low", "reason": "device_switch"}),
            ]
            switches = [(e["data"]["to"], e["data"]["by"]) for e in events(http, "device.switch")]
            assert switches == [("hermes", "first"), ("laptop", "rest"), ("hermes", "device")]
            assert http.get("/health").json()["devices_connected"] == 2
        assert first.quit() == 0 and laptop.quit() == 0
    finally:
        stop(*server)
