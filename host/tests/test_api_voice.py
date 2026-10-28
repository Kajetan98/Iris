import asyncio
import json
import logging
import struct

import pytest

from host.api.service import HostService
from host.brain import Assistant, LLMUnavailable
from host.brain.audio import STT_RATE, OpusDecoder, resample
from host.brain.speech import SpeechError
from host.core.clock import ManualClock
from host.core.config import Config, LLMConfig
from host.store import Store
from host.tests.audio_helpers import dominant_hz, header, opus_packets
from host.tests.brain_helpers import ScriptedEngine
from host.tests.helpers import heartbeat
from host.tests.test_api_service import FakeWS, frame, settle
from host.tests.test_brain_voice import TTS_HZ, FakeSTT, FakeTTS

import numpy as np

LLM_CFG = LLMConfig.model_validate({"engine": "ollama", "model": "m"})
run = asyncio.run


def config(**audio):
    # Czekanie na koniec odtwarzania ma osobny test; tu tylko spowalniałoby kolejne tury.
    audio.setdefault("serialize_playback", False)
    return Config.model_validate({"brain": {"audio": audio}, "api": {"ack_timeout_s": 0.3}})


def make(*, replies=("Mam się dobrze.",), stt=None, tts=None, cfg=None, assistant=True):
    clock = ManualClock()
    store = Store(clock=clock)
    engine = ScriptedEngine(replies)
    service = HostService(
        cfg or config(), clock, store,
        assistant=Assistant(engine, LLM_CFG) if assistant else None,
        stt=stt or FakeSTT(), tts=tts or FakeTTS(),
    )
    service.start()
    return service, engine, store


async def connect(service):
    ws = FakeWS()
    task = asyncio.create_task(service.serve_connection(ws))
    await settle()
    return ws, task


def say(ws, utt_id, packets, dur_ms=1500):
    ws.feed_text(frame("audio.utterance.start", {"utt_id": utt_id}))
    for seq, p in enumerate(packets):
        ws.feed_bytes(struct.pack(">BHH", 1, utt_id & 0xFFFF, seq) + p)
    ws.feed_text(frame("audio.utterance.end", {"utt_id": utt_id, "dur_ms": dur_ms}))


def speak_actions(ws):
    return [json.loads(t) for t in ws.sent if json.loads(t)["type"] == "speak"]


async def until(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("warunek nie został spełniony w czasie")


async def answer_speak(ws, ok=True, err=None, n=1):
    """Czeka na n-tą akcję speak i odsyła ack."""
    await until(lambda: len(speak_actions(ws)) >= n)
    action = speak_actions(ws)[n - 1]
    body = {"ref": action["id"], "ok": ok}
    if err:
        body["err"] = err
    ws.feed_text(frame("speak", body, t="ack"))


async def finish_turns(service):
    await asyncio.gather(*list(service._voice_tasks), return_exceptions=True)


# --- tura głosowa ---


def test_full_voice_turn_over_the_websocket():
    async def scenario():
        service, engine, store = make()
        service.core.handle_event(heartbeat(71))
        ws, task = await connect(service)
        say(ws, 7, opus_packets(440, 1.5))
        await answer_speak(ws)
        await finish_turns(service)

        stt = service.stt
        pcm, rate = stt.calls[0]
        assert rate == STT_RATE and abs(len(pcm) / 2 / rate - 1.5) < 0.06 and abs(dominant_hz(pcm) - 440) < 5
        # transkrypcja trafiła do LLM razem z kontekstem
        turn = engine.calls[0][1][-1].content
        assert turn.endswith("[WYPOWIEDŹ UŻYTKOWNIKA]\nJak się masz") and "Tryb pracy: normalny" in turn
        assert "bateria: brak danych → 71%" in turn
        # ramki kind=2 (binarne) przed akcją speak (tekst), potem nic więcej
        kinds = [k for k, _ in ws.log]
        assert kinds == ["bytes"] * len(ws.sent_bytes) + ["text"]
        assert [header(f) for f in ws.sent_bytes] == [(2, 1, i) for i in range(len(ws.sent_bytes))]
        assert json.loads(ws.sent[0]) | {"id": None, "ts": None} == {
            "v": 1, "t": "action", "id": None, "ts": None, "type": "speak", "body": {"audio_id": 1},
        }
        # to, co odbierze ciało, da się zdekodować do tonu z TTS
        dec = OpusDecoder()
        out = resample(np.concatenate([dec.decode(f[5:]) for f in ws.sent_bytes]), 48000, STT_RATE)
        assert abs(dominant_hz(out) - TTS_HZ) < 5 and abs(out.size / STT_RATE - 1.0) < 0.1
        assert "action.sent" in [e.type for e in store.events()]
        ws.disconnect()
        await task

    run(scenario())


def test_two_turns_get_increasing_audio_ids_and_keep_history():
    async def scenario():
        service, engine, _ = make(replies=["Pierwsza.", "Druga."])
        ws, task = await connect(service)
        for n, utt in enumerate((1, 2), start=1):
            say(ws, utt, opus_packets(440, 1.0))
            await answer_speak(ws, n=n)
            await finish_turns(service)
        assert [a["body"]["audio_id"] for a in speak_actions(ws)] == [1, 2]
        history = engine.chat_calls[1][1]
        assert [m.role for m in history] == ["user", "assistant", "user"]
        ws.disconnect()
        await task

    run(scenario())


def test_utterance_counts_as_interaction_and_ends_long_silence():
    async def scenario():
        service, _, clock_store = make()
        service.clock.advance(4 * 3600 + 1)
        service.core.handle_event(heartbeat(71))
        service.tick()
        assert "long_silence" in service.core.drives.active()
        ws, task = await connect(service)
        ws.feed_text(frame("audio.utterance.start", {"utt_id": 1}))
        await settle()
        assert "long_silence" not in service.core.drives.active()
        ws.disconnect()
        await task

    run(scenario())


# --- wyłączony głos ---


def test_voice_disabled_without_engines():
    async def scenario():
        clock = ManualClock()
        service = HostService(config(), clock, Store(clock=clock))
        service.start()
        ws, task = await connect(service)
        say(ws, 7, opus_packets(440, 1.0))
        await settle(30)
        assert service.voice is None and ws.log == [] and service.health()["voice"] == "disabled"
        ws.disconnect()
        await task

    run(scenario())


def test_voice_needs_the_llm():
    service, _, _ = make(assistant=False)
    assert service.voice is None and service.health()["voice"] == "error"


def test_ready_status_when_pipeline_is_built():
    service, _, _ = make()
    assert service.health()["voice"] == "ready"


# --- błędy tury ---


class BrokenSTT(FakeSTT):
    async def transcribe(self, pcm, rate):
        raise SpeechError("model STT padł")


class BrokenTTS(FakeTTS):
    async def synthesize(self, text):
        raise SpeechError("głos TTS padł")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stt": BrokenSTT()},
        {"tts": BrokenTTS()},
        {"replies": [LLMUnavailable("brak LLM")]},
    ],
    ids=["stt", "tts", "llm"],
)
def test_failed_turn_plays_the_error_clip(kwargs, caplog):
    async def scenario():
        service, _, _ = make(cfg=config(error_clip="error"), **kwargs)
        ws, task = await connect(service)
        say(ws, 7, opus_packets(440, 1.0))
        await until(lambda: any(json.loads(t)["type"] == "speak.clip" for t in ws.sent))
        await finish_turns(service)
        clip = next(json.loads(t) for t in ws.sent if json.loads(t)["type"] == "speak.clip")
        assert clip["body"] == {"clip": "error"} and ws.sent_bytes == []
        ws.disconnect()
        await task

    with caplog.at_level(logging.WARNING):
        run(scenario())
    assert any("tura głosowa nieudana" in r.getMessage() for r in caplog.records)


def test_failed_turn_is_silent_without_error_clip():
    async def scenario():
        service, _, _ = make(stt=BrokenSTT())
        ws, task = await connect(service)
        say(ws, 7, opus_packets(440, 1.0))
        await settle(20)
        await finish_turns(service)
        assert ws.log == []
        ws.disconnect()
        await task

    run(scenario())


@pytest.mark.parametrize("packets_s, text", [(0.2, "Jak się masz"), (1.0, "  ")], ids=["za-krotka", "pusta-transkrypcja"])
def test_no_reply_and_no_clip_for_noise(packets_s, text):
    async def scenario():
        service, engine, _ = make(stt=FakeSTT(text), cfg=config(error_clip="error"))
        ws, task = await connect(service)
        say(ws, 7, opus_packets(440, packets_s))
        await settle(20)
        await finish_turns(service)
        assert ws.log == [] and engine.calls == []
        ws.disconnect()
        await task

    run(scenario())


def test_llm_failure_in_a_voice_turn_rolls_back_the_state_slice():
    async def scenario():
        service, engine, _ = make(replies=[LLMUnavailable("brak"), "Już działa."])
        service.core.handle_event(heartbeat(71))
        ws, task = await connect(service)
        say(ws, 1, opus_packets(440, 1.0))
        await settle(30)
        await finish_turns(service)
        say(ws, 2, opus_packets(440, 1.0))
        await answer_speak(ws)
        await finish_turns(service)
        first, second = (engine.calls[i][1][-1].content for i in (0, 1))
        assert "bateria: brak danych → 71%" in first and first == second
        ws.disconnect()
        await task

    run(scenario())


def test_connection_lost_during_the_turn_is_handled(caplog):
    async def scenario():
        gate = asyncio.Event()

        class Slow(FakeSTT):
            async def transcribe(self, pcm, rate):
                await gate.wait()
                return await super().transcribe(pcm, rate)

        service, _, _ = make(stt=Slow(), cfg=config(error_clip="error"))
        ws, task = await connect(service)
        say(ws, 7, opus_packets(440, 1.0))
        await settle(30)
        ws.disconnect()
        await task
        gate.set()
        await finish_turns(service)
        assert ws.sent_bytes == [] and not any(json.loads(t)["type"] == "speak.clip" for t in ws.sent)

    with caplog.at_level(logging.WARNING):
        run(scenario())
    assert any("NotConnected" in r.getMessage() for r in caplog.records)


def test_rejected_or_unanswered_speak_is_only_logged(caplog):
    async def scenario():
        service, _, _ = make(replies=["a.", "b."], cfg=config(error_clip="error"))
        ws, task = await connect(service)
        say(ws, 1, opus_packets(440, 1.0))
        await answer_speak(ws, ok=False, err="no audio for id")
        await finish_turns(service)
        say(ws, 2, opus_packets(440, 1.0))
        await until(lambda: len(speak_actions(ws)) >= 2)
        await finish_turns(service)  # drugi speak nie dostaje ack: timeout 0.3 s
        assert not any(json.loads(t)["type"] == "speak.clip" for t in ws.sent)
        ws.disconnect()
        await task

    with caplog.at_level(logging.WARNING):
        run(scenario())
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "ciało odrzuciło speak 1: no audio for id" in messages and "speak 2: AckTimeout" in messages


# --- współbieżność i zamykanie ---


def test_pending_turn_limit_drops_extra_utterances(caplog):
    async def scenario():
        gate = asyncio.Event()

        class Slow(FakeSTT):
            async def transcribe(self, pcm, rate):
                await gate.wait()
                return await super().transcribe(pcm, rate)

        service, _, _ = make(stt=Slow(), cfg=config(max_pending_turns=1))
        ws, task = await connect(service)
        say(ws, 1, opus_packets(440, 1.0))
        await settle(30)
        say(ws, 2, opus_packets(440, 1.0))
        await settle(30)
        assert len(service._voice_tasks) == 1 and not service.voice.assembler.active
        gate.set()
        await answer_speak(ws)
        await finish_turns(service)
        assert len(service.stt.calls) == 1  # druga wypowiedź została pominięta
        ws.disconnect()
        await task

    with caplog.at_level(logging.WARNING):
        run(scenario())
    assert any("zbyt wiele tur" in r.getMessage() for r in caplog.records)


def test_drain_voice_cancels_running_turns():
    async def scenario():
        class Hanging(FakeSTT):
            async def transcribe(self, pcm, rate):
                await asyncio.Event().wait()

        service, _, _ = make(stt=Hanging(), cfg=config(error_clip="error"))
        ws, task = await connect(service)
        say(ws, 7, opus_packets(440, 1.0))
        await settle(30)
        assert len(service._voice_tasks) == 1
        await asyncio.wait_for(service.drain_voice(), 1)
        assert not service._voice_tasks and ws.log == []  # anulowanie nie gra klipu błędu
        ws.disconnect()
        await task

    run(scenario())


def test_concurrent_replies_are_played_one_after_another():
    async def scenario():
        service, _, _ = make(replies=["Pierwsza.", "Druga."], tts=FakeTTS(0.3, delay=0.01))
        ws, task = await connect(service)
        say(ws, 1, opus_packets(440, 1.0))
        say(ws, 2, opus_packets(440, 1.0))
        await until(lambda: len(speak_actions(ws)) >= 1)
        await answer_speak(ws, n=1)
        await until(lambda: len(speak_actions(ws)) >= 2)
        await answer_speak(ws, n=2)
        await finish_turns(service)
        ids = [header(f)[1] for f in ws.sent_bytes]
        assert ids == sorted(ids)  # ramki odpowiedzi 1 przed ramkami odpowiedzi 2
        ws.disconnect()
        await task

    run(scenario())


def test_host_waits_for_playback_before_the_next_reply():
    async def scenario():
        service, _, _ = make(replies=["Pierwsza.", "Druga."], tts=FakeTTS(0.5), cfg=config(serialize_playback=True))
        ws, task = await connect(service)
        loop = asyncio.get_running_loop()
        say(ws, 1, opus_packets(440, 1.0))
        say(ws, 2, opus_packets(440, 1.0))
        await answer_speak(ws, n=1)
        acked_at = loop.time()
        await until(lambda: any(header(f)[1] == 2 for f in ws.sent_bytes), timeout=3)
        assert loop.time() - acked_at >= 0.45  # ramki drugiej odpowiedzi dopiero po 0,5 s odtwarzania
        await answer_speak(ws, n=2)
        await finish_turns(service)
        ws.disconnect()
        await task

    run(scenario())


# --- prywatność ---


def test_conversation_never_reaches_logs_or_the_database(caplog):
    async def scenario():
        service, _, store = make(
            stt=FakeSTT("PYTANIE-TAJNE-55"), replies=["ODPOWIEDZ-TAJNA-55."], cfg=config(error_clip="error")
        )
        ws, task = await connect(service)
        say(ws, 1, opus_packets(440, 1.0))
        await answer_speak(ws)
        await finish_turns(service)
        dump = []
        for table in ("sessions", "events", "state_snapshots", "facts"):
            dump += [str(r) for r in store._conn.execute(f"SELECT * FROM {table}")]
        assert "TAJNE-55" not in "\n".join(dump) and "TAJNA-55" not in "\n".join(dump)
        ws.disconnect()
        await task

    with caplog.at_level(logging.DEBUG):
        run(scenario())
    assert "TAJNE-55" not in "\n".join(r.getMessage() for r in caplog.records)
    assert "TAJNA-55" not in "\n".join(r.getMessage() for r in caplog.records)


# --- cykl życia aplikacji ---

from fastapi.testclient import TestClient  # noqa: E402

from host.api.main import create_app  # noqa: E402


class Lifecycle:
    """Mixin śledzący load/aclose silnika."""

    fail_load = False

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.loaded = 0
        self.closed = False

    def load(self):
        self.loaded += 1
        if self.fail_load:
            raise RuntimeError("model uszkodzony")

    async def aclose(self):
        self.closed = True


class TrackedSTT(Lifecycle, FakeSTT):
    pass


class TrackedTTS(Lifecycle, FakeTTS):
    pass


def llm_config(extra=None):
    return Config.model_validate(
        {"brain": {"llm": {"engine": "ollama", "model": "m"}, **(extra or {})}, "store": {"path": ":memory:"}}
    )


VOICE_CFG = {
    "stt": {"engine": "faster-whisper", "model": "small"},
    "tts": {"engine": "piper", "voice": "/g.onnx"},
}


def test_injected_engines_are_loaded_but_not_closed_by_the_app():
    stt, tts = TrackedSTT(), TrackedTTS()
    app = create_app(llm_config(), ManualClock(), assistant=Assistant(ScriptedEngine(), LLM_CFG), stt=stt, tts=tts, run_tick_loop=False)
    with TestClient(app) as c:
        assert c.get("/health").json()["voice"] == "ready"
        assert (stt.loaded, tts.loaded) == (1, 1)
    assert not stt.closed and not tts.closed


def test_engines_built_from_config_are_loaded_and_closed(monkeypatch):
    stt, tts = TrackedSTT(), TrackedTTS()
    monkeypatch.setattr("host.brain.build_stt", lambda cfg: stt)
    monkeypatch.setattr("host.brain.build_tts", lambda cfg: tts)
    monkeypatch.setattr("host.brain.build_assistant", lambda cfg: Assistant(ScriptedEngine(), cfg))
    app = create_app(llm_config(VOICE_CFG), ManualClock(), run_tick_loop=False)
    with TestClient(app) as c:
        assert c.get("/health").json()["voice"] == "ready"
        assert (stt.loaded, tts.loaded) == (1, 1) and not stt.closed
    assert stt.closed and tts.closed


def test_model_load_failure_disables_voice_but_not_the_host(monkeypatch, caplog):
    class BadSTT(TrackedSTT):
        fail_load = True

    stt, tts = BadSTT(), TrackedTTS()
    monkeypatch.setattr("host.brain.build_stt", lambda cfg: stt)
    monkeypatch.setattr("host.brain.build_tts", lambda cfg: tts)
    monkeypatch.setattr("host.brain.build_assistant", lambda cfg: Assistant(ScriptedEngine(["Tekstowo działa."]), cfg))
    app = create_app(llm_config(VOICE_CFG), ManualClock(), run_tick_loop=False)
    with caplog.at_level(logging.ERROR):
        with TestClient(app) as c:
            assert c.get("/health").json()["voice"] == "error"
            assert c.get("/self").status_code == 200  # warstwa stanu działa
            assert c.post("/chat", json={"text": "Cześć"}).json()["text"] == "Tekstowo działa."
    assert any("głos wyłączony" in r.getMessage() for r in caplog.records)
    assert stt.closed and tts.closed  # niewykorzystane silniki są zamknięte


def test_voice_configured_without_llm_is_an_error_not_a_crash():
    cfg = Config.model_validate({"brain": VOICE_CFG, "store": {"path": ":memory:"}})
    app = create_app(cfg, ManualClock(), stt=TrackedSTT(), tts=TrackedTTS(), run_tick_loop=False)
    with TestClient(app) as c:
        assert c.get("/health").json()["voice"] == "error"
        assert c.get("/self").status_code == 200


def test_health_reports_disabled_voice_by_default():
    app = create_app(Config.model_validate({"store": {"path": ":memory:"}}), ManualClock(), run_tick_loop=False)
    with TestClient(app) as c:
        assert c.get("/health").json()["voice"] == "disabled"
