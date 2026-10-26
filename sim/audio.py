"""Dźwięk symulatora: ton, odczyt WAV i kodowanie do pakietów Opus (docs/spec.md, sekcja 6).

Wymaga numpy i PyAV (z libopus); ładowane dopiero przy komendach `say` i `sayfile`.
Nie importuje niczego z `host`: kodowanie ma być niezależnym sprawdzianem spec.
"""

from __future__ import annotations

import math
import wave
from fractions import Fraction
from typing import Any


class AudioUnavailable(Exception):
    pass


def _libs() -> tuple[Any, Any]:
    try:
        import av
        import numpy as np
    except ImportError as e:
        raise AudioUnavailable("brak PyAV lub numpy (pip install av numpy)") from e
    return av, np


def tone(freq_hz: float, seconds: float, rate: int, amplitude: float = 0.4) -> Any:
    _, np = _libs()
    t = np.arange(int(seconds * rate)) / rate
    return (amplitude * np.sin(2 * math.pi * freq_hz * t) * 32767).astype(np.int16)


def read_wav(path: str) -> tuple[Any, int]:
    """WAV PCM 8 lub 16 bitów -> (próbki int16 mono, częstotliwość)."""
    _, np = _libs()
    try:
        with wave.open(path, "rb") as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(frames)
    except (OSError, EOFError, wave.Error) as e:
        raise AudioUnavailable(f"nie można odczytać WAV: {e}") from None
    if width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.int32)
    elif width == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.int32) - 128) << 8
    else:
        raise AudioUnavailable(f"obsługiwany jest tylko WAV PCM 8/16 bitów, plik ma {8 * width}")
    if channels > 1:
        samples = samples[: len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
    return samples.astype(np.int16), rate


def encode_opus(samples: Any, rate: int, *, target_rate: int, frame_ms: float, bitrate_bps: int) -> list[bytes]:
    """int16 mono w dowolnej częstotliwości -> pakiety Opus (mono, target_rate, pakiety frame_ms)."""
    av, np = _libs()
    if rate != target_rate and len(samples):
        resampler = av.AudioResampler(format="s16", layout="mono", rate=target_rate)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = rate
        parts = [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
        parts += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
        samples = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
    if len(samples) == 0:
        return []
    ctx = av.CodecContext.create("libopus", "w")
    ctx.sample_rate = target_rate
    ctx.layout = "mono"
    ctx.format = "s16"
    ctx.bit_rate = bitrate_bps
    ctx.time_base = Fraction(1, target_rate)
    ctx.options = {"frame_duration": f"{frame_ms:g}", "application": "voip"}
    ctx.open()
    n = ctx.frame_size
    pad = (-len(samples)) % n
    if pad:
        samples = np.concatenate([samples, np.zeros(pad, dtype=np.int16)])
    packets: list[bytes] = []
    for i in range(0, len(samples), n):
        frame = av.AudioFrame.from_ndarray(samples[i : i + n].reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = target_rate
        frame.pts = i
        packets += [bytes(p) for p in ctx.encode(frame)]
    packets += [bytes(p) for p in ctx.encode(None)]
    return packets
