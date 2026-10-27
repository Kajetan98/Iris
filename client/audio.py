"""Dźwięk programu na komputer: Opus ramka po ramce, wykrywanie mowy po głośności, wejście i wyjście.

PyAV (libopus) i numpy są wymagane; sounddevice (PortAudio) dopiero przy prawdziwym sprzęcie.
Nie importuje niczego z `host` (program działa na innej maszynie niż host).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from fractions import Fraction
from typing import Any, Protocol

import av
import numpy as np

RATE = 16000  # spec, sekcja 6
FRAME = RATE // 50  # 20 ms
BITRATE = 24000
DECODE_RATE = 48000  # tyle daje libopus


class AudioUnavailable(Exception):
    pass


class OpusStream:
    """Koder jednej wypowiedzi: ramki po 20 ms na wejściu, pakiety Opus na wyjściu."""

    def __init__(self) -> None:
        ctx = av.CodecContext.create("libopus", "w")
        ctx.sample_rate = RATE
        ctx.layout = "mono"
        ctx.format = "s16"
        ctx.bit_rate = BITRATE
        ctx.time_base = Fraction(1, RATE)
        ctx.options = {"frame_duration": "20", "application": "voip"}
        ctx.open()
        self._ctx = ctx
        self._pts = 0

    def encode(self, frame: np.ndarray) -> list[bytes]:
        """Ramka FRAME próbek int16 (krótsza jest dopełniana ciszą)."""
        if frame.size < FRAME:
            frame = np.concatenate([frame, np.zeros(FRAME - frame.size, dtype=np.int16)])
        f = av.AudioFrame.from_ndarray(frame[:FRAME].astype(np.int16).reshape(1, -1), format="s16", layout="mono")
        f.sample_rate = RATE
        f.pts = self._pts
        self._pts += FRAME
        return [bytes(p) for p in self._ctx.encode(f)]

    def flush(self) -> list[bytes]:
        return [bytes(p) for p in self._ctx.encode(None)]


def decode_packets(packets: list[bytes | None]) -> np.ndarray:
    """Pakiety odpowiedzi w kolejności seq; brakujący (None) to 20 ms ciszy. PCM int16, 48 kHz."""
    ctx = av.CodecContext.create("libopus", "r")
    ctx.sample_rate = DECODE_RATE
    ctx.layout = "mono"
    ctx.open()
    gap = np.zeros(DECODE_RATE // 50, dtype=np.int16)
    parts = []
    for packet in packets:
        if not packet:
            parts.append(gap)
            continue
        for frame in ctx.decode(av.Packet(packet)):
            parts.append(frame.to_ndarray().reshape(-1).astype(np.int16))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)


def dbfs(frame: np.ndarray) -> float:
    rms = math.sqrt(float(np.mean(frame.astype(np.float64) ** 2))) if frame.size else 0.0
    return 20 * math.log10(rms / 32768) if rms > 0 else -120.0


class EnergyVad:
    """Wypowiedź od kilku głośnych ramek do ciszy trwającej end_ms. Bez słowa wybudzającego.

    feed() zwraca listę zdarzeń: ("start", ramki sprzed startu), ("frame", ramka), ("end", None)."""

    def __init__(self, threshold_dbfs: float = -40.0, start_frames: int = 3, end_ms: int = 800,
                 preroll_ms: int = 200) -> None:
        self.threshold = threshold_dbfs
        self.start_frames = start_frames
        self.end_frames = max(1, end_ms // 20)
        self._preroll: deque[np.ndarray] = deque(maxlen=max(1, preroll_ms // 20))
        self._loud = 0
        self._quiet = 0
        self.speaking = False

    def feed(self, frame: np.ndarray) -> list[tuple[str, Any]]:
        loud = dbfs(frame) > self.threshold
        if not self.speaking:
            self._preroll.append(frame)
            self._loud = self._loud + 1 if loud else 0
            if self._loud < self.start_frames:
                return []
            self.speaking, self._quiet = True, 0
            before = list(self._preroll)
            self._preroll.clear()
            return [("start", before)]
        self._quiet = 0 if loud else self._quiet + 1
        if self._quiet >= self.end_frames:
            self.speaking, self._loud = False, 0
            return [("frame", frame), ("end", None)]
        return [("frame", frame)]


class AudioBackend(Protocol):
    def open_input(self, on_frame: Callable[[np.ndarray], None]) -> None:
        """Ramki FRAME próbek int16 z mikrofonu; on_frame wołane z wątku audio."""

    def close_input(self) -> None: ...

    def play(self, pcm: np.ndarray, rate: int) -> None:
        """Odtwarza i wraca po końcu (wołane w osobnym wątku)."""


class SoundDeviceBackend:
    """Mikrofon i głośnik systemu przez PortAudio (pakiet sounddevice)."""

    def __init__(self, input_device: str | int | None = None, output_device: str | int | None = None) -> None:
        try:
            import sounddevice as sd
        except (ImportError, OSError) as e:  # OSError: brak biblioteki PortAudio
            raise AudioUnavailable(
                f"sounddevice niedostępne ({e}); pip install sounddevice, na Linuksie także libportaudio2"
            ) from e
        self._sd = sd
        self._input_device = input_device
        self._output_device = output_device
        self._stream: Any = None

    def open_input(self, on_frame: Callable[[np.ndarray], None]) -> None:
        def callback(indata: Any, frames: int, time: Any, status: Any) -> None:
            on_frame(indata[:, 0].copy())

        self._stream = self._sd.InputStream(
            samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME, device=self._input_device, callback=callback,
        )
        self._stream.start()

    def close_input(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def play(self, pcm: np.ndarray, rate: int) -> None:
        with self._sd.OutputStream(samplerate=rate, channels=1, dtype="int16", device=self._output_device) as out:
            out.write(pcm.reshape(-1, 1))
