"""Pełny tor głosowy: sim (Opus) -> host -> STT -> LLM -> TTS -> Opus -> sim.

Na prawdziwych gniazdach i prawdziwym Opus. Zaślepki zostają tylko dla modeli STT i TTS.
"""

import asyncio

import httpx
import numpy as np
import pytest

from host.api.main import create_app
from host.brain.audio import STT_RATE, OpusDecoder, resample
from host.core.config import Config
from host.tests.audio_helpers import dominant_hz, sine, write_wav
from host.tests.servers import free_port, ollama_stub_app, start, stop, until
from host.tests.test_brain_voice import TTS_HZ, FakeSTT, FakeTTS
from sim.body import SimRunner
from sim.model import SimConfig

REPLY = "Mam się dobrze. Dziękuję za pytanie."


@pytest.fixture
def stack():
    ollama_app, ollama = ollama_stub_app(REPLY)
    ollama_port = free_port()
    ollama_server = start(ollama_app, ollama_port)

    cfg = Config.model_validate(
        {
            "store": {"path": ":memory:"},
            "link": {"timeout_s": 0.8, "recover_s": 0.4},
            "api": {"tick_interval_s": 0.1, "ack_timeout_s": 3.0},
            "brain": {
                "llm": {"engine": "ollama", "model": "bielik", "retries": 0, "timeout_s": 3,
                        "base_url": f"http://127.0.0.1:{ollama_port}"},
                "audio": {"error_clip": "error"},
            },
        }
    )
    stt, tts = FakeSTT("Jak się masz"), FakeTTS(0.6)
    app = create_app(cfg, stt=stt, tts=tts)
    port = free_port()
    host_server = start(app, port)
    yield SimpleStack(port, app, ollama, stt, tts)
    stop(*host_server)
    stop(*ollama_server)


class SimpleStack:
    def __init__(self, port, app, ollama, stt, tts):
        self.port, self.app, self.ollama, self.stt, self.tts = port, app, ollama, stt, tts


def decode_playback(packets):
    dec = OpusDecoder()
    return resample(np.concatenate([dec.decode(p) for p in packets]), 48000, STT_RATE)


def test_voice_conversation_through_the_whole_stack(stack, tmp_path):
    async def scenario():
        sim = SimRunner(
            SimConfig(heartbeat_interval_s=0.1),
            f"ws://127.0.0.1:{stack.port}/ws",
            say=lambda s: None,
        )
        task = asyncio.create_task(sim.run())
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{stack.port}", timeout=10) as http:

            async def link_ok():
                return (await http.get("/health")).json()["link"]["state"] == "ok"

            await until(link_ok)
            assert (await http.get("/health")).json()["voice"] == "ready"

            # 1. wypowiedź (ton 440 Hz w Opus) -> odpowiedź odtworzona na ciele
            sim.command("say 1500")
            await until(lambda: len(sim.model.played_audio) == 1)
            pcm, rate = stack.stt.calls[0]
            assert rate == STT_RATE and abs(len(pcm) / 2 / rate - 1.5) < 0.06
            assert abs(dominant_hz(pcm) - 440) < 5  # STT dostał to, co wysłało ciało
            llm_turn = stack.ollama["requests"][-1]["messages"][-1]["content"]
            assert llm_turn.endswith("[WYPOWIEDŹ UŻYTKOWNIKA]\nJak się masz") and "Tryb pracy:" in llm_turn
            audio_id, packets = sim.model.played_audio[0]
            played = decode_playback(packets)
            assert audio_id == 1 and abs(dominant_hz(played) - TTS_HZ) < 5
            assert abs(played.size / STT_RATE - 1.2) < 0.1  # dwa zdania po 0,6 s
            assert sim.model.played[0][0] == 1

            # 2. nagranie z pliku WAV (22,05 kHz, stereo) idzie tą samą drogą
            wav = tmp_path / "pytanie.wav"
            write_wav(wav, sine(300, 1.0, 22050), 22050, channels=2)
            sim.command(f"sayfile {wav}")
            await until(lambda: len(sim.model.played_audio) == 2)
            pcm, _ = stack.stt.calls[1]
            assert abs(dominant_hz(pcm) - 300) < 5 and abs(len(pcm) / 2 / STT_RATE - 1.0) < 0.06
            assert sim.model.played_audio[1][0] == 2
            assert [m["role"] for m in stack.ollama["requests"][-1]["messages"]] == [
                "system", "user", "assistant", "user",
            ]  # historia rozmowy obejmuje pierwszą turę

            # 3. awaria LLM: ciało dostaje klip błędu, a nie ciszę
            stack.ollama["fail"] = True
            sim.command("say 1000")
            await until(lambda: sim.model.clips_played == ["error"])
            assert len(sim.model.played_audio) == 2
            stack.ollama["fail"] = False

            # 4. po awarii wszystko działa dalej
            sim.command("say 1000")
            await until(lambda: len(sim.model.played_audio) == 3)

            # 5. w bazie są akcje speak, ale nie ma treści rozmowy
            r = await http.get("/events", params=[("type", "action.sent"), ("type", "action.ack")])
            events = r.json()["events"]
            speaks = [e for e in events if e["type"] == "action.sent" and e["data"]["type"] == "speak"]
            assert [e["data"]["body"]["audio_id"] for e in speaks] == [1, 2, 3]
            assert all(e["data"]["ok"] for e in events if e["type"] == "action.ack")
            everything = (await http.get("/events", params={"limit": 1000})).text
            assert "Jak się masz" not in everything and "Dziękuję" not in everything

        sim.stop()
        await asyncio.wait_for(task, 3)

    asyncio.run(scenario())
    service = stack.app.state.service
    assert service.rejected_frames == 0  # ramki symulatora i hosta przeszły walidację po obu stronach
