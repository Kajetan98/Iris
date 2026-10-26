"""Dźwięk: Opus <-> PCM i składanie wypowiedzi (docs/spec.md, sekcja 6).

Wymaga numpy i PyAV (z libopus); moduł jest importowany tylko przy włączonym głosie.
"""

from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable

import numpy as np

from host.core.config import AudioConfig
from host.core.protocol import AudioFrame

log = logging.getLogger(__name__)

STT_RATE = 16000
OPUS_DECODE_RATE = 48000
KIND_PLAYBACK = 2
MAX_AUDIO_ID = 65535
_HEADER = struct.Struct(">BHH")  # kind, id, seq (big-endian)


class AudioError(Exception):
    pass


def _av() -> Any:
    try:
        import av
    except ImportError as e:
        raise AudioError("brak biblioteki PyAV (pip install av)") from e
    return av


def resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """int16 mono -> int16 mono w innej częstotliwości próbkowania."""
    if src_rate == dst_rate or pcm.size == 0:
        return pcm
    av = _av()
    resampler = av.AudioResampler(format="s16", layout="mono", rate=dst_rate)
    frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = src_rate
    parts = [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
    parts += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)


class OpusDecoder:
    """Dekoder jednego strumienia. Zwraca PCM int16 mono 48 kHz (tyle daje libopus)."""

    def __init__(self) -> None:
        av = _av()
        self._av = av
        self._ctx = av.CodecContext.create("libopus", "r")
        self._ctx.sample_rate = OPUS_DECODE_RATE
        self._ctx.layout = "mono"
        self._ctx.open()

    def decode(self, packet: bytes) -> np.ndarray:
        """Pusta tablica oznacza pakiet, z którego nic nie wyszło.

        Pusty pakiet nie trafia do ffmpeg: dla niego oznacza koniec strumienia i po nim dekoder
        odrzuca wszystkie kolejne pakiety."""
        if not packet:
            return np.zeros(0, dtype=np.int16)
        try:
            frames = self._ctx.decode(self._av.Packet(packet))
        except self._av.error.FFmpegError as e:
            raise AudioError(f"pakiet Opus nie do zdekodowania: {e}") from None
        parts = [f.to_ndarray().reshape(-1) for f in frames]
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)


class OpusEncoder:
    """Koder jednego strumienia (mono). Po encode() nie nadaje się do ponownego użycia."""

    def __init__(self, rate: int, frame_ms: float, bitrate_bps: int) -> None:
        av = _av()
        self._av = av
        self.rate = rate
        self.frame_samples = int(round(rate * frame_ms / 1000))
        ctx = av.CodecContext.create("libopus", "w")
        ctx.sample_rate = rate
        ctx.layout = "mono"
        ctx.format = "s16"
        ctx.bit_rate = bitrate_bps
        ctx.time_base = Fraction(1, rate)
        ctx.options = {"frame_duration": f"{frame_ms:g}", "application": "voip"}
        ctx.open()
        if ctx.frame_size != self.frame_samples:
            raise AudioError(f"koder ma ramkę {ctx.frame_size} próbek, oczekiwano {self.frame_samples}")
        self._ctx = ctx

    def encode(self, pcm: np.ndarray) -> list[bytes]:
        n = self.frame_samples
        if pcm.size == 0:
            return []
        pad = (-pcm.size) % n
        if pad:
            pcm = np.concatenate([pcm, np.zeros(pad, dtype=np.int16)])
        packets: list[bytes] = []
        for i in range(0, pcm.size, n):
            frame = self._av.AudioFrame.from_ndarray(
                pcm[i : i + n].reshape(1, -1), format="s16", layout="mono"
            )
            frame.sample_rate = self.rate
            frame.pts = i
            packets += [bytes(p) for p in self._ctx.encode(frame)]
        packets += [bytes(p) for p in self._ctx.encode(None)]
        return packets


def encode_speech(pcm: bytes, rate: int, cfg: AudioConfig) -> list[bytes]:
    """PCM s16le mono w dowolnej częstotliwości -> pakiety Opus według konfiguracji odtwarzania."""
    samples = resample(np.frombuffer(pcm, dtype=np.int16), rate, cfg.playback_rate_hz)
    return OpusEncoder(cfg.playback_rate_hz, cfg.frame_ms, cfg.bitrate_bps).encode(samples)


def playback_frames(audio_id: int, packets: list[bytes]) -> list[bytes]:
    """Ramki binarne `kind=2` dla odtwarzania na ciele."""
    if not 1 <= audio_id <= MAX_AUDIO_ID:
        raise AudioError(f"audio_id poza zakresem 1-{MAX_AUDIO_ID}: {audio_id}")
    if len(packets) > 0xFFFF + 1:
        raise AudioError("zbyt wiele pakietów na jedno odtworzenie")
    return [_HEADER.pack(KIND_PLAYBACK, audio_id, seq) + p for seq, p in enumerate(packets)]


def frame_id_for(utt_id: int | str) -> int | None:
    """Młodsze 16 bitów utt_id, czyli `id` ramek mikrofonu. None, gdy utt_id nie jest liczbą."""
    if isinstance(utt_id, bool):
        return None
    try:
        return int(utt_id) & 0xFFFF
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RawUtterance:
    utt_id: int | str
    packets: dict[int, bytes]
    truncated: bool
    declared_ms: float | None


@dataclass(frozen=True)
class Utterance:
    utt_id: int | str
    pcm: bytes  # s16le mono 16 kHz
    duration_ms: float
    packets: int
    lost: int
    truncated: bool


class UtteranceAssembler:
    """Składa pakiety mikrofonu w wypowiedź. Obsługuje jedną wypowiedź naraz.

    Stan zmienia się tylko w start/add/take (pętla zdarzeń); decode() jest czystą funkcją
    i może pracować w wątku.
    """

    def __init__(
        self, cfg: AudioConfig, decoder_factory: Callable[[], OpusDecoder] = OpusDecoder
    ) -> None:
        self._cfg = cfg
        self._factory = decoder_factory
        self._max_packets = max(1, math.ceil(cfg.max_utterance_s * 1000 / cfg.frame_ms))
        self._reset()
        self.dropped_frames = 0

    def _reset(self) -> None:
        self._utt_id: int | str | None = None
        self._key: int | None = None
        self._packets: dict[int, bytes] = {}
        self._truncated = False

    @property
    def active(self) -> bool:
        return self._key is not None

    def start(self, utt_id: int | str) -> bool:
        if self._key is not None:
            log.warning("porzucono niedokończoną wypowiedź (nowy start)")
        self._reset()
        key = frame_id_for(utt_id)
        if key is None:
            log.warning("utt_id nie jest liczbą, wypowiedź pominięta")
            return False
        self._utt_id, self._key = utt_id, key
        return True

    def add(self, frame: AudioFrame) -> bool:
        if self._key is None or frame.id != self._key:
            self.dropped_frames += 1
            return False
        if frame.seq in self._packets:
            return False
        if len(self._packets) >= self._max_packets:
            self._truncated = True
            return False
        self._packets[frame.seq] = frame.data
        return True

    def take(self, utt_id: int | str, declared_ms: float | None = None) -> RawUtterance | None:
        """Kończy wypowiedź i oddaje surowe pakiety. None, gdy to nie ta wypowiedź lub jest pusta."""
        if self._key is None or frame_id_for(utt_id) != self._key:
            log.warning("koniec wypowiedzi bez pasującego początku")
            return None
        raw = RawUtterance(self._utt_id, self._packets, self._truncated, declared_ms)  # type: ignore[arg-type]
        self._reset()
        return raw if raw.packets else None

    def decode(self, raw: RawUtterance) -> Utterance | None:
        """Dekoduje pakiety, zastępuje braki ciszą i sprowadza dźwięk do 16 kHz."""
        decoder = self._factory()
        seqs = sorted(raw.packets)
        unit = int(OPUS_DECODE_RATE * self._cfg.frame_ms / 1000)
        parts: list[np.ndarray] = []
        lost = 0
        for seq in range(seqs[0], seqs[-1] + 1):
            samples = None
            data = raw.packets.get(seq)
            if data is not None:
                try:
                    samples = decoder.decode(data)
                except AudioError:
                    samples = None
            if samples is None or samples.size == 0:
                lost += 1
                parts.append(np.zeros(unit, dtype=np.int16))
            else:
                unit = samples.size
                parts.append(samples)
        pcm = resample(np.concatenate(parts), OPUS_DECODE_RATE, STT_RATE)
        duration_ms = pcm.size * 1000 / STT_RATE
        if duration_ms < self._cfg.min_utterance_ms:
            log.debug("wypowiedź za krótka (%.0f ms)", duration_ms)
            return None
        return Utterance(
            raw.utt_id, pcm.tobytes(), duration_ms, len(raw.packets), lost, raw.truncated
        )
