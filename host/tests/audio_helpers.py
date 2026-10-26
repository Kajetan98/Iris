"""Pomocniki testów audio: prawdziwy Opus, sygnały testowe."""

import math
import struct

import numpy as np

from host.brain.audio import STT_RATE, OpusEncoder
from host.core.protocol import AudioFrame


def sine(freq_hz, seconds, rate, amp=0.4):
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * math.pi * freq_hz * t) * 32767).astype(np.int16)


def dominant_hz(pcm, rate=STT_RATE):
    x = pcm.astype(np.float64) if isinstance(pcm, np.ndarray) else np.frombuffer(pcm, np.int16).astype(np.float64)
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.argmax(spectrum) * rate / len(x))


def opus_packets(freq_hz=440, seconds=1.0, rate=16000, frame_ms=20):
    return OpusEncoder(rate, frame_ms, 24000).encode(sine(freq_hz, seconds, rate))


def mic_frame(utt_id, seq, packet):
    return AudioFrame(1, utt_id & 0xFFFF, seq, packet)


def feed(assembler_or_pipeline, utt_id, packets, drop=()):
    start = getattr(assembler_or_pipeline, "start", None) or assembler_or_pipeline.on_utterance_start
    add = getattr(assembler_or_pipeline, "add", None) or assembler_or_pipeline.on_audio
    start(utt_id)
    for seq, p in enumerate(packets):
        if seq not in drop:
            add(mic_frame(utt_id, seq, p))


def header(frame):
    return struct.unpack(">BHH", frame[:5])
