"""Potok głosowy: wypowiedź -> STT -> odpowiedź -> TTS -> Opus -> ciało (docs/spec.md, sekcja 6).

Transport podaje się funkcjami, więc potok nie zależy od FastAPI ani od HostService.
Do logów trafiają tylko czasy i liczby, nigdy treść rozmowy.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np

from host.brain.audio import (
    MAX_AUDIO_ID,
    STT_RATE,
    OpusDecoder,
    RawUtterance,
    UtteranceAssembler,
    encode_speech,
    playback_frames,
    resample,
)
from host.brain.speech import SpeechError, STTEngine, Transcript, TTSEngine
from host.core.config import AudioConfig
from host.core.protocol import AudioFrame

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True)
class Spoken:
    audio_id: int
    packets: int
    duration_s: float
    sentences: int
    shortened: bool  # odpowiedź skrócona do max_speech_s


@dataclass(frozen=True)
class VoiceTurn:
    transcript: Transcript
    reply: str
    spoken: Spoken


def fit_body_buffer(
    packets: list[bytes], audio: AudioConfig, limits: tuple[int, int] | None = None
) -> list[bytes]:
    """Najdłuższy początek odpowiedzi, który mieści się w buforze odtwarzania ciała.

    limits: (bajty, pakiety) urządzenia; liczy się mniejsza z wartości urządzenia i konfiguracji."""
    max_bytes, max_packets = audio.body_buffer_bytes, audio.body_max_packets
    if limits is not None:
        max_bytes, max_packets = min(max_bytes, limits[0]), min(max_packets, limits[1])
    total = 0
    for i, p in enumerate(packets[:max_packets]):
        total += len(p)
        if total > max_bytes:
            return packets[:i]
    return packets[:max_packets]


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text.strip()) if s.strip()]


class VoicePipeline:
    def __init__(
        self,
        *,
        stt: STTEngine,
        tts: TTSEngine,
        respond: Callable[[str], Awaitable[str]],
        send_frame: Callable[[bytes], Awaitable[None]],
        announce: Callable[[int], Awaitable[None]],
        audio: AudioConfig,
        min_text_chars: int = 2,
        decoder_factory: Callable[[], OpusDecoder] = OpusDecoder,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        limits: Callable[[], tuple[int, int] | None] = lambda: None,
        heard: Callable[[str], Awaitable[None]] | None = None,
        replying: Callable[[str, bool], Awaitable[None]] | None = None,
    ) -> None:
        self._stt = stt
        self._tts = tts
        self._respond = respond
        self._send_frame = send_frame
        self._announce = announce
        self._audio = audio
        self._min_text_chars = min_text_chars
        self._sleep = sleep
        self._limits = limits  # bufor odtwarzania urządzenia, które dostanie odpowiedź
        # Napisy (spec, sekcja 6): rozpoznany tekst i tekst, który zaraz zostanie wypowiedziany.
        self._heard = heard
        self._replying = replying
        self.assembler = UtteranceAssembler(audio, decoder_factory)
        self._say_lock = asyncio.Lock()
        self._next_audio_id = 1

    # --- wejście z ciała (wołane z pętli zdarzeń) ---

    def on_utterance_start(self, utt_id: int | str) -> None:
        self.assembler.start(utt_id)

    def on_audio(self, frame: AudioFrame) -> None:
        self.assembler.add(frame)

    def take_utterance(self, utt_id: int | str, dur_ms: float | None = None) -> RawUtterance | None:
        """Kończy składanie wypowiedzi. Wołać synchronicznie przy audio.utterance.end, zanim
        zacznie się następna wypowiedź; dalszą obróbkę (process) można odłożyć do zadania."""
        return self.assembler.take(utt_id, dur_ms)

    async def on_utterance_end(self, utt_id: int | str, dur_ms: float | None = None) -> VoiceTurn | None:
        raw = self.take_utterance(utt_id, dur_ms)
        return None if raw is None else await self.process(raw)

    async def process(self, raw: RawUtterance) -> VoiceTurn | None:
        """Pełna tura. None, gdy wypowiedź była pusta, za krótka lub bez rozpoznanego tekstu."""
        started = time.monotonic()
        utterance = await asyncio.to_thread(self.assembler.decode, raw)
        if utterance is None:
            return None
        if utterance.lost or utterance.truncated:
            log.warning(
                "wypowiedź: %d pakietów, %d utraconych, ucięta=%s",
                utterance.packets, utterance.lost, utterance.truncated,
            )
        t_stt = time.monotonic()
        transcript = await self._stt.transcribe(utterance.pcm, STT_RATE)
        text = transcript.text.strip()
        stt_ms = (time.monotonic() - t_stt) * 1000
        if len(text) < self._min_text_chars:
            log.info("pusta transkrypcja (%.0f ms), brak odpowiedzi", stt_ms)
            return None
        if self._heard is not None:
            await self._heard(text)
        t_llm = time.monotonic()
        reply = (await self._respond(text)).strip()
        llm_ms = (time.monotonic() - t_llm) * 1000
        t_tts = time.monotonic()
        spoken = await self.say(reply)
        log.info(
            "tura głosowa: wypowiedź %.0f ms, stt %.0f ms, odpowiedź %.0f ms, tts+wysyłka %.0f ms, "
            "razem %.0f ms, %d znaków -> %.1f s mowy",
            utterance.duration_ms, stt_ms, llm_ms, (time.monotonic() - t_tts) * 1000,
            (time.monotonic() - started) * 1000, len(text), spoken.duration_s,
        )
        return VoiceTurn(transcript, reply, spoken)

    # --- wyjście na ciało ---

    async def say(self, text: str) -> Spoken:
        """Syntezuje tekst (do max_speech_s), wysyła ramki kind=2 i zapowiada je akcją speak."""
        async with self._say_lock:
            pcm, sentences, shortened = await self._synthesize_limited(text)
            packets = await asyncio.to_thread(encode_speech, pcm, self._audio.playback_rate_hz, self._audio)
            fitted = fit_body_buffer(packets, self._audio, self._limits())
            if len(fitted) < len(packets):
                log.warning("odpowiedź ucięta do %d z %d pakietów (bufor ciała)", len(fitted), len(packets))
                shortened = True
                pcm = pcm[: int(len(fitted) * self._audio.frame_ms / 1000 * self._audio.playback_rate_hz) * 2]
            packets = fitted
            if self._replying is not None:
                spoken_text = " ".join(split_sentences(text)[:sentences])
                await self._replying(spoken_text, shortened)
            audio_id = self._allocate_audio_id()
            for frame in playback_frames(audio_id, packets):
                await self._send_frame(frame)
            await self._announce(audio_id)
            duration_s = len(pcm) / 2 / self._audio.playback_rate_hz
            if self._audio.serialize_playback:
                # Ciało ma jeden bufor odtwarzania: następne ramki dopiero, gdy to się skończy.
                await self._sleep(duration_s)
            return Spoken(audio_id, len(packets), duration_s, sentences, shortened)

    async def _synthesize_limited(self, text: str) -> tuple[bytes, int, bool]:
        rate = self._audio.playback_rate_hz
        limit = int(self._audio.max_speech_s * rate)
        chunks: list[np.ndarray] = []
        total = 0
        shortened = False
        sentences = split_sentences(text)
        for index, sentence in enumerate(sentences):
            speech = await self._tts.synthesize(sentence)
            samples = await asyncio.to_thread(
                resample, np.frombuffer(speech.pcm, dtype=np.int16), speech.rate, rate
            )
            if chunks and total + samples.size > limit:
                shortened = True
                log.warning("odpowiedź skrócona do %d z %d zdań (limit %.0f s)", index, len(sentences), self._audio.max_speech_s)
                break
            if samples.size > limit:  # pierwsze zdanie dłuższe niż limit
                samples = samples[:limit]
                shortened = True
            chunks.append(samples)
            total += samples.size
        if not chunks:
            raise SpeechError("pusty tekst do wypowiedzenia")
        return np.concatenate(chunks).tobytes(), len(chunks), shortened

    def _allocate_audio_id(self) -> int:
        audio_id = self._next_audio_id
        self._next_audio_id = audio_id % MAX_AUDIO_ID + 1
        return audio_id
