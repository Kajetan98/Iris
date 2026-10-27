"""Program na komputer i Raspberry Pi (client/): protokół, Opus, wykrywanie mowy, rozmowa z hostem.

Kontener nie ma kart dźwiękowych, więc tor audio idzie przez atrapę backendu. Prawdziwy backend
(sounddevice, PortAudio) jest sprawdzany na urządzeniu ALSA "null", jeśli PortAudio jest dostępne.
"""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import httpx
import numpy as np
import pytest

from client.audio import DECODE_RATE, FRAME, RATE, EnergyVad, OpusStream, decode_packets
from client.desktop import DesktopBody, DesktopRunner, desktop_config
from host.api.main import create_app
from host.brain.audio import STT_RATE
from host.core.config import Config
from host.tests.audio_helpers import dominant_hz, sine
from host.tests.servers import free_port, ollama_stub_app, start, stop, until
from host.tests.test_brain_voice import TTS_HZ, FakeSTT, FakeTTS

ROOT = Path(__file__).parents[2]


class FakeAudio:
    def __init__(self, hold=None):
        self.on_frame = None
        self.played = []
        self.closed = False
        self._hold = hold  # threading.Event: odtwarzanie trwa, dopóki test go nie ustawi

    def open_input(self, on_frame):
        self.on_frame = on_frame

    def close_input(self):
        self.closed = True

    def play(self, pcm, rate):
        if self._hold is not None:
            self._hold.wait(5)
        self.played.append((pcm, rate))

    def speak(self, samples):
        for i in range(0, len(samples), FRAME):
            self.on_frame(samples[i:i + FRAME])


def body(audio=None, battery=None, vad=None):
    b = DesktopBody(desktop_config("laptop", battery), 0.0, audio or FakeAudio(), battery=lambda: battery, vad=vad)
    b.take_out()
    return b


def frames(model):
    return [json.loads(o.data) for o in model.take_out() if isinstance(o.data, str)]


def action(type_, body_, id_="h1"):
    return json.dumps({"v": 1, "t": "action", "id": id_, "ts": 0, "type": type_, "body": body_})


def test_caps_and_heartbeat_contain_only_real_data():
    b = body()
    caps = json.loads(b.on_connect(0.0).data)["body"]
    assert caps == {"has": ["mic", "speaker", "display"], "clips": [], "playback_bytes": 1048576,
                    "playback_packets": 4096}
    for t in range(0, 61):
        b.tick(float(t))
    sent = frames(b)
    assert {f["type"] for f in sent} == {"body.heartbeat"}  # bez body.sensor: nie zmyślamy temperatury
    assert sent[0]["body"] == {"frame_errors": 0}  # bez RSSI i bez baterii
    with_battery = body(battery=55.0)
    assert json.loads(with_battery.on_connect(0.0).data)["body"]["has"] == ["mic", "speaker", "display", "battery"]
    with_battery.tick(1.0)
    assert frames(with_battery)[0]["body"]["batt_pct"] == 55


@pytest.mark.parametrize(
    "type_, body_, err",
    [
        ("haptic", {"pattern": "warn"}, "unsupported"),
        ("haptic", {"pattern": "loud"}, "bad_value"),
        ("gpio.set", {"pin": 5, "value": 1}, "unsupported"),
        ("gpio.set", {"pin": "5", "value": 1}, "bad_body"),
        ("speak.clip", {"clip": "ready"}, "unknown_clip"),
        ("speak", {"audio_id": 3}, "no_audio"),
        ("speak", {"audio_id": True}, "bad_body"),
        ("ping", {}, None),
        ("config.set", {"key": "volume", "value": 40}, None),
        ("display", {"turn": 1, "kind": "heard", "text": "Cześć"}, None),
        ("display", {"turn": 1, "kind": "heard"}, "bad_body"),
        ("display", {"turn": 1, "kind": "hello"}, "bad_value"),
    ],
)
def test_acks(type_, body_, err):
    b = body()
    b.on_host_text(action(type_, body_), 1.0)
    (ack,) = frames(b)
    assert (ack["body"]["ok"], ack["body"].get("err")) == (err is None, err)


def test_inactive_device_does_not_start_utterances():
    b = body()
    b.on_host_text(action("device.active", {"active": False}), 1.0)
    frames(b)
    assert b.command("", 1.0) == "urządzenie nieaktywne, brak wypowiedzi"
    assert frames(b) == [] and b.talking is None
    assert b.command("teleport", 1.0).startswith("nieznana komenda")
    assert "aktywne: nie" in b.command("status", 1.0)


def test_opus_stream_round_trip():
    tone = sine(440, 1.0, RATE)
    enc = OpusStream()
    packets = [p for i in range(0, tone.size, FRAME) for p in enc.encode(tone[i:i + FRAME])] + enc.flush()
    pcm = decode_packets(packets)
    assert abs(dominant_hz(pcm, rate=DECODE_RATE) - 440) < 5
    assert abs(pcm.size / DECODE_RATE - 1.0) < 0.05
    with_gap = decode_packets([packets[0], None, packets[2]])
    assert with_gap.size == 3 * DECODE_RATE // 50 and not with_gap[960:1920].any()  # brak pakietu to cisza


def test_energy_vad_finds_one_utterance():
    vad = EnergyVad(threshold_dbfs=-40, start_frames=3, end_ms=400, preroll_ms=100)
    silence = np.zeros(FRAME, dtype=np.int16)
    tone = sine(300, 0.5, RATE)
    events = [e for _ in range(20) for e in vad.feed(silence)]
    assert events == []
    for i in range(0, tone.size, FRAME):
        events += vad.feed(tone[i:i + FRAME])
    for _ in range(30):
        events += vad.feed(silence)
    kinds = [k for k, _ in events]
    assert kinds.count("start") == 1 and kinds.count("end") == 1
    assert len(events[0][1]) == 5  # 100 ms przed startem
    assert kinds.index("end") == len(kinds) - 1 - kinds[::-1].index("end")


def test_busy_while_playing():
    async def scenario():
        hold = threading.Event()
        b = body(audio=FakeAudio(hold))
        packets = OpusStream().encode(sine(300, 0.02, RATE)) + [b"\xf8\xff\xfe"]
        for seq, p in enumerate(packets):
            b.on_host_binary(bytes([2, 0, 1, 0, seq]) + p)
        b.on_host_text(action("speak", {"audio_id": 1}), 1.0)
        assert b.playing
        b.on_host_binary(bytes([2, 0, 2, 0, 0]) + packets[0])
        b.on_host_text(action("speak", {"audio_id": 2}, id_="h2"), 1.0)
        acks = [f["body"] for f in frames(b)]
        assert acks == [{"ref": "h1", "ok": True}, {"ref": "h2", "ok": False, "err": "busy"}]
        assert b.busy_drops == 1
        hold.set()
        await until(lambda: not b.playing)
        assert b.played[0][0] == 1

    asyncio.run(scenario())


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
    yield port, stt
    stop(*server)
    stop(*ollama)


def run_client(port, scenario, *, vad=None):
    async def main():
        audio = FakeAudio()
        b = DesktopBody(desktop_config("laptop", None, heartbeat_interval_s=0.2), 0.0, audio, vad=vad, show=shown.append)
        runner = DesktopRunner(b, f"ws://127.0.0.1:{port}/ws?device=laptop", say=lambda s: None)
        task = asyncio.create_task(runner.run())
        await until(lambda: runner.connected)
        try:
            await scenario(runner, audio)
        finally:
            runner.stop()
            await task
        assert audio.closed  # mikrofon zamknięty po zakończeniu
        return audio

    shown = []
    audio = asyncio.run(main())
    return audio, shown


def test_push_to_talk_conversation_with_the_host(voice_host):
    port, stt = voice_host

    async def scenario(runner, audio):
        runner.command("")  # Enter: start
        audio.speak(sine(440, 1.5, RATE))
        await asyncio.sleep(0.2)
        runner.command("")  # Enter: koniec
        await until(lambda: audio.played, timeout=10)

    audio, shown = run_client(port, scenario)
    assert shown == ["Rozpoznano: Jak się masz", "IRIS: Mam się dobrze. Dziękuję za pytanie."]
    pcm, rate = stt.calls[0]
    assert rate == STT_RATE and abs(len(pcm) / 2 / rate - 1.5) < 0.06
    assert abs(dominant_hz(pcm) - 440) < 5
    played, rate = audio.played[0]
    assert rate == DECODE_RATE and abs(dominant_hz(played, rate=rate) - TTS_HZ) < 5
    assert abs(played.size / rate - 1.2) < 0.1
    devices = httpx.get(f"http://127.0.0.1:{port}/devices").json()["devices"]
    assert devices[0]["caps"]["has"] == ["mic", "speaker", "display"] and devices[0]["rssi"] is None


def test_hands_free_with_vad(voice_host):
    port, stt = voice_host

    async def scenario(runner, audio):
        audio.speak(np.zeros(RATE // 2, dtype=np.int16))
        audio.speak(sine(440, 1.0, RATE))
        audio.speak(np.zeros(RATE, dtype=np.int16))
        await until(lambda: audio.played, timeout=10)

    run_client(port, scenario, vad=EnergyVad(end_ms=500))
    pcm, rate = stt.calls[0]
    seconds = len(pcm) / 2 / rate
    assert 1.0 < seconds < 1.9, seconds  # mowa z krótkim wstępem i ciszą na końcu
    assert abs(dominant_hz(pcm) - 440) < 5


def test_sounddevice_backend_on_alsa_null_device(tmp_path):
    asound = tmp_path / "asound.conf"
    asound.write_text("pcm.!default { type null }\npcm.null { type null }\n")
    code = textwrap.dedent("""
        import threading, numpy as np
        from client.audio import SoundDeviceBackend, AudioUnavailable
        try:
            backend = SoundDeviceBackend()
        except AudioUnavailable as e:
            print("SKIP", e); raise SystemExit(0)
        if not backend._sd.query_devices():
            print("SKIP brak urządzeń"); raise SystemExit(0)
        got = []
        done = threading.Event()
        def on_frame(frame):
            got.append(frame.shape)
            if len(got) >= 5: done.set()
        backend.open_input(on_frame)
        done.wait(5)
        backend.close_input()
        backend.play(np.zeros(4800, dtype=np.int16), 48000)
        print("OK", got[0], len(got) >= 5)
    """)
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                       env={**os.environ, "ALSA_CONFIG_PATH": str(asound)})
    out = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr
    if out.startswith("SKIP"):
        pytest.skip(out)
    assert r.returncode == 0 and out == "OK (320,) True", r.stderr[-500:]
