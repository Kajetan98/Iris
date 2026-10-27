"""Napisy tury głosowej: akcja display (docs/spec.md, sekcje 5 i 6)."""

import asyncio
import json
import logging

import pytest

from host.brain.speech import SpeechError
from host.core.protocol import encode_action
from host.tests.audio_helpers import opus_packets
from host.tests.test_api_devices import actions, caps_frame, connect, voice_service
from host.tests.test_api_service import settle
from host.tests.test_api_voice import say, until

run = asyncio.run


def displays(ws):
    return [a["body"] for a in actions(ws, "display")]


async def turn_done(service):
    await until(lambda: service._voice_tasks)
    await asyncio.gather(*list(service._voice_tasks))


def test_display_validation_and_frame_limit():
    assert encode_action("display", {"turn": 1, "kind": "nothing"}, id="h1", ts_ms=0)
    for bad in ({"turn": 1, "kind": "heard"}, {"turn": 1, "kind": "failed", "text": "x"},
                {"turn": 1, "kind": "reply", "text": "x", "truncated": "nie"}, {"turn": 1, "kind": "hello"}):
        with pytest.raises(ValueError):
            encode_action("display", bad, id="h1", ts_ms=0)
    encode_action("display", {"turn": 1, "kind": "reply", "text": "ż" * 1900}, id="h1", ts_ms=0)  # 3800 B
    with pytest.raises(ValueError):
        encode_action("display", {"turn": 1, "kind": "reply", "text": "ż" * 2100}, id="h1", ts_ms=0)
    with pytest.raises(ValueError):
        encode_action("speak.clip", {"clip": "x" * 600}, id="h1", ts_ms=0)  # pozostałe akcje: 512 B


def test_captions_come_before_the_audio_and_never_reach_the_store_or_logs(caplog):
    async def scenario():
        service = voice_service()
        ws, task = await connect(service, "telefon")
        ws.feed_text(caps_frame(has=["mic", "speaker", "display"]))
        say(ws, 7, opus_packets(440, 1.0))
        await turn_done(service)
        assert displays(ws) == [
            {"turn": 7, "kind": "heard", "text": "Jak się masz"},
            {"turn": 7, "kind": "reply", "text": "Dobrze.", "truncated": False},
        ]
        kinds = [("text", json.loads(d)["type"]) if k == "text" else ("bytes", None) for k, d in ws.log]
        reply_at = kinds.index(("text", "display"), kinds.index(("text", "display")) + 1)
        assert kinds.index(("bytes", None)) > reply_at  # napis przed ramkami audio
        assert kinds.index(("text", "speak")) > reply_at
        logged = [e.data["body"] for e in service.store.events(types=["action.sent"]) if e.data["type"] == "display"]
        assert logged == [{"turn": 7, "kind": "heard", "chars": 12}, {"turn": 7, "kind": "reply", "truncated": False, "chars": 7}]
        everything = json.dumps([e.data for e in service.store.events()], ensure_ascii=False)
        assert "Jak się masz" not in everything and "Dobrze." not in everything
        ws.disconnect()
        await task

    with caplog.at_level(logging.DEBUG):
        run(scenario())
    assert "Jak się masz" not in caplog.text and "Dobrze." not in caplog.text


def test_devices_without_display_get_no_captions():
    async def scenario():
        service = voice_service()
        ws, task = await connect(service, "hermes")
        ws.feed_text(caps_frame(has=["mic", "speaker"]))
        say(ws, 1, opus_packets(440, 1.0))
        await turn_done(service)
        assert displays(ws) == [] and actions(ws, "speak")
        ws.disconnect()
        await task

    run(scenario())


def test_nothing_and_failed_turns():
    async def scenario():
        service = voice_service()
        ws, task = await connect(service, "telefon")
        ws.feed_text(caps_frame(has=["mic", "speaker", "display"]))
        service.stt.text = ""
        say(ws, 1, opus_packets(440, 1.0))
        await until(lambda: len(displays(ws)) == 1)

        async def broken(pcm, rate):
            raise SpeechError("stt nie działa")

        service.stt.transcribe = broken
        say(ws, 2, opus_packets(440, 1.0))
        await until(lambda: len(displays(ws)) == 2)
        assert displays(ws) == [{"turn": 1, "kind": "nothing"}, {"turn": 2, "kind": "failed"}]
        ws.disconnect()
        await task

    run(scenario())


def test_reply_caption_has_only_the_spoken_sentences():
    async def scenario():
        service = voice_service()
        service.assistant.engine._replies = ["Pierwsze zdanie. Drugie zdanie. Trzecie zdanie."]
        service.voice._audio = service.voice._audio.model_copy(update={"max_speech_s": 1.0})
        ws, task = await connect(service, "telefon")
        ws.feed_text(caps_frame(has=["mic", "speaker", "display"]))
        say(ws, 3, opus_packets(440, 1.0))
        await turn_done(service)
        reply = displays(ws)[-1]
        assert reply == {"turn": 3, "kind": "reply", "text": "Pierwsze zdanie. Drugie zdanie.", "truncated": True}
        ws.disconnect()
        await task

    run(scenario())


def test_switch_command_caption_goes_to_the_new_device():
    async def scenario():
        service = voice_service()
        service.stt.text = "przełącz na telefon"
        hermes, t1 = await connect(service, "hermes")
        phone, t2 = await connect(service, "telefon")
        phone.feed_text(caps_frame(has=["mic", "speaker", "display"]))
        await settle()
        say(hermes, 1, opus_packets(440, 1.0))
        await until(lambda: displays(phone))
        await asyncio.gather(*list(service._voice_tasks))
        assert displays(phone) == [{"turn": 1, "kind": "reply", "text": "Przełączam na telefon.", "truncated": False}]
        for ws, t in ((hermes, t1), (phone, t2)):
            ws.disconnect()
            await t

    run(scenario())
