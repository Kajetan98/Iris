import asyncio
import logging

import numpy as np
import pytest

from host.brain.audio import STT_RATE, OpusDecoder, resample
from host.brain.speech import Speech, SpeechError, Transcript
from host.brain.voice import VoicePipeline, split_sentences
from host.core.config import AudioConfig
from host.tests.audio_helpers import dominant_hz, feed, header, opus_packets, sine

run = asyncio.run
TTS_HZ = 300


class FakeSTT:
    def __init__(self, text="Jak się masz"):
        self.text = text
        self.calls = []

    async def transcribe(self, pcm, rate):
        self.calls.append((pcm, rate))
        return Transcript(self.text, "pl", len(pcm) / 2 / rate)

    async def aclose(self):
        pass


class FakeTTS:
    """Każde zdanie to ton TTS_HZ trwający seconds_per_sentence (lub z tabeli po tekście)."""

    def __init__(self, seconds=1.0, rate=22050, by_text=None, delay=0.0):
        self.seconds, self.rate, self.by_text, self.delay = seconds, rate, by_text or {}, delay
        self.calls = []

    async def synthesize(self, text):
        self.calls.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        return Speech(sine(TTS_HZ, self.by_text.get(text, self.seconds), self.rate).tobytes(), self.rate)

    async def aclose(self):
        pass


class Rig:
    def __init__(self, *, stt=None, tts=None, reply="Mam się dobrze.", audio=None, min_text_chars=2, send_delay=0.0):
        self.stt = stt or FakeSTT()
        self.tts = tts or FakeTTS()
        self.events = []  # ("frame", (kind, id, seq)) i ("announce", audio_id)
        self.frames = []
        self.asked = []
        self.reply = reply

        async def respond(text):
            self.asked.append(text)
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

        async def send_frame(frame):
            await asyncio.sleep(send_delay)  # jak prawdziwy transport: wysyłka oddaje sterowanie pętli
            self.frames.append(frame)
            self.events.append(("frame", header(frame)))

        async def announce(audio_id):
            self.events.append(("announce", audio_id))

        self.pipeline = VoicePipeline(
            stt=self.stt, tts=self.tts, respond=respond, send_frame=send_frame,
            announce=announce, audio=audio or AudioConfig(), min_text_chars=min_text_chars,
        )

    def utter(self, packets=None, utt_id=7, drop=()):
        feed(self.pipeline, utt_id, packets if packets is not None else opus_packets(440, 1.5), drop)
        return run(self.pipeline.on_utterance_end(utt_id, 1500))

    def played_pcm(self):
        dec = OpusDecoder()
        out = np.concatenate([dec.decode(f[5:]) for f in self.frames])
        return resample(out, 48000, STT_RATE)


# --- pełna tura ---


def test_full_turn_with_real_opus_both_ways():
    rig = Rig()
    turn = rig.utter()
    # wejście: STT dostaje 16 kHz s16le z poprawnie zdekodowanym tonem
    pcm, rate = rig.stt.calls[0]
    assert rate == STT_RATE and abs(len(pcm) / 2 / rate - 1.5) < 0.06
    assert abs(dominant_hz(pcm) - 440) < 5
    assert rig.asked == ["Jak się masz"]
    # wyjście: ramki kind=2 z rosnącym seq, potem speak
    kinds = [e[0] for e in rig.events]
    assert kinds == ["frame"] * (len(rig.events) - 1) + ["announce"]
    assert rig.events[-1] == ("announce", 1)
    frame_headers = [e[1] for e in rig.events[:-1]]
    assert frame_headers == [(2, 1, seq) for seq in range(len(frame_headers))]
    # ramki da się zdekodować do tonu z TTS
    out = rig.played_pcm()
    assert abs(dominant_hz(out) - TTS_HZ) < 5 and abs(out.size / STT_RATE - 1.0) < 0.1
    assert (turn.transcript.text, turn.reply) == ("Jak się masz", "Mam się dobrze.")
    assert (turn.spoken.audio_id, turn.spoken.sentences, turn.spoken.shortened) == (1, 1, False)
    assert turn.spoken.packets == len(frame_headers) and turn.spoken.duration_s == pytest.approx(1.0, abs=0.01)


def test_lost_packets_do_not_stop_the_turn():
    rig = Rig()
    turn = rig.utter(drop={3, 4, 20})
    assert turn is not None and rig.asked == ["Jak się masz"]
    assert abs(len(rig.stt.calls[0][0]) / 2 / STT_RATE - 1.5) < 0.06


# --- wypowiedzi, na które nie ma odpowiedzi ---


def test_too_short_utterance_gets_no_reply():
    rig = Rig()
    assert rig.utter(opus_packets(440, 0.2)) is None
    assert rig.stt.calls == [] and rig.events == []


def test_end_without_start_gets_no_reply():
    rig = Rig()
    assert run(rig.pipeline.on_utterance_end(7, 1000)) is None
    assert rig.stt.calls == []


def test_non_numeric_utterance_id_is_ignored():
    rig = Rig()
    rig.pipeline.on_utterance_start("abc")
    assert run(rig.pipeline.on_utterance_end("abc", 1000)) is None


@pytest.mark.parametrize("text", ["", "   ", "a"])
def test_empty_transcript_gets_no_reply(text):
    rig = Rig(stt=FakeSTT(text))
    assert rig.utter() is None
    assert rig.asked == [] and rig.events == [] and rig.tts.calls == []


def test_min_text_chars_is_configurable():
    rig = Rig(stt=FakeSTT("a"), min_text_chars=1)
    assert rig.utter() is not None


# --- limit długości odpowiedzi ---


REPLY = "Pierwsze zdanie. Drugie zdanie. Trzecie zdanie."


def test_reply_within_limit_is_spoken_in_full():
    rig = Rig(tts=FakeTTS(0.5), reply=REPLY, audio=AudioConfig(max_speech_s=2.0))
    spoken = rig.utter().spoken
    assert (spoken.sentences, spoken.shortened) == (3, False)
    assert rig.tts.calls == ["Pierwsze zdanie.", "Drugie zdanie.", "Trzecie zdanie."]


def test_reply_is_shortened_at_sentence_boundary():
    rig = Rig(tts=FakeTTS(0.9), reply=REPLY, audio=AudioConfig(max_speech_s=2.0))
    spoken = rig.utter().spoken
    assert (spoken.sentences, spoken.shortened) == (2, True)
    assert spoken.duration_s == pytest.approx(1.8, abs=0.02)
    assert len(rig.tts.calls) == 3  # trzecie zdanie zsyntezowano, ale nie weszło do odtwarzania


def test_first_sentence_longer_than_limit_is_cut():
    rig = Rig(tts=FakeTTS(3.0), reply="Jedno bardzo długie zdanie.", audio=AudioConfig(max_speech_s=1.0))
    spoken = rig.utter().spoken
    assert spoken.shortened and spoken.sentences == 1
    assert spoken.duration_s == pytest.approx(1.0, abs=0.01)
    assert abs(rig.played_pcm().size / STT_RATE - 1.0) < 0.1


def test_playback_follows_audio_config():
    cfg = AudioConfig(playback_rate_hz=8000, frame_ms=40, bitrate_bps=12000)
    rig = Rig(audio=cfg)
    rig.utter()
    assert abs(len(rig.frames) - 26) <= 2  # 1 s w pakietach po 40 ms


# --- identyfikatory odtwarzania ---


def test_audio_ids_increase_and_wrap():
    rig = Rig()
    ids = [rig.utter(utt_id=i).spoken.audio_id for i in (1, 2, 3)]
    assert ids == [1, 2, 3]
    rig.pipeline._next_audio_id = 65535
    assert run(rig.pipeline.say("Test.")).audio_id == 65535
    assert run(rig.pipeline.say("Test.")).audio_id == 1


def test_say_works_without_an_utterance():
    rig = Rig()
    spoken = run(rig.pipeline.say("Powiedz coś."))
    assert spoken.audio_id == 1 and rig.events[-1] == ("announce", 1)
    assert rig.asked == []


def test_say_rejects_empty_text():
    rig = Rig()
    with pytest.raises(SpeechError):
        run(rig.pipeline.say("  "))
    assert rig.events == []


def test_concurrent_says_do_not_interleave_frames():
    async def scenario():
        # Wysyłka trwa dłużej niż kodowanie drugiej odpowiedzi, więc bez blokady ramki by się przeplotły.
        rig = Rig(tts=FakeTTS(0.3, delay=0.01), send_delay=0.002)
        await asyncio.gather(rig.pipeline.say("Pierwsza."), rig.pipeline.say("Druga."))
        ids = [e[1][1] if e[0] == "frame" else -e[1] for e in rig.events]
        # ramki jednej odpowiedzi, jej speak, dopiero potem następna
        assert ids == [1] * ids.index(-1) + [-1] + [2] * (len(ids) - ids.index(-1) - 2) + [-2]

    run(scenario())


# --- błędy ---


def test_tts_failure_sends_nothing():
    class Broken(FakeTTS):
        async def synthesize(self, text):
            raise SpeechError("brak głosu")

    rig = Rig(tts=Broken())
    with pytest.raises(SpeechError):
        rig.utter()
    assert rig.events == []


def test_respond_failure_propagates_before_any_audio():
    rig = Rig(reply=RuntimeError("LLM padł"))
    with pytest.raises(RuntimeError):
        rig.utter()
    assert rig.events == [] and rig.tts.calls == []


def test_stt_failure_propagates():
    class Broken(FakeSTT):
        async def transcribe(self, pcm, rate):
            raise SpeechError("model")

    rig = Rig(stt=Broken())
    with pytest.raises(SpeechError):
        rig.utter()
    assert rig.asked == []


def test_pipeline_recovers_after_failed_turn():
    rig = Rig(reply=RuntimeError("LLM padł"))
    with pytest.raises(RuntimeError):
        rig.utter(utt_id=1)
    rig.reply = "Już działa."
    assert rig.utter(utt_id=2) is not None


# --- prywatność ---


def test_conversation_text_never_reaches_logs(caplog):
    rig = Rig(stt=FakeSTT("PYTANIE-TAJNE-91"), reply="ODPOWIEDZ-TAJNA-91.")
    with caplog.at_level(logging.DEBUG):
        rig.utter(drop={2})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "TAJNE-91" not in text and "TAJNA-91" not in text
    assert any("tura głosowa" in r.getMessage() for r in caplog.records)  # są za to czasy i liczby


# --- podział na zdania ---


@pytest.mark.parametrize(
    "text, parts",
    [
        ("Jedno.", ["Jedno."]),
        ("Pierwsze. Drugie! Trzecie? Czwarte…  Piąte", ["Pierwsze.", "Drugie!", "Trzecie?", "Czwarte…", "Piąte"]),
        ("  Bez kropki  ", ["Bez kropki"]),
        ("Liczba 3.5 jest ok. Dalej.", ["Liczba 3.5 jest ok.", "Dalej."]),
        ("", []),
    ],
)
def test_split_sentences(text, parts):
    assert split_sentences(text) == parts
