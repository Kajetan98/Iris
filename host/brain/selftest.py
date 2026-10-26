"""Samotest toru głosowego na prawdziwych modelach.

    python -m host.brain.selftest --config iris.toml [--text "Dzień dobry, jak się masz?"]

Piper czyta tekst, dźwięk idzie przez Opus i składanie wypowiedzi jak z mikrofonu ciała,
a Whisper go rozpoznaje. Pierwsze uruchomienie pobiera model Whispera z Hugging Face
(host huggingface.co musi być dostępny albo trzeba podać katalog z modelem).
Kod wyjścia 0, gdy odsetek błędnych słów nie przekracza progu.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time

import numpy as np

from host.brain import build_stt, build_tts
from host.brain.audio import STT_RATE, OpusEncoder, UtteranceAssembler, resample
from host.brain.speech import SpeechError, STTEngine, TTSEngine
from host.core.config import Config
from host.core.protocol import AudioFrame

DEFAULT_TEXT = "Dzień dobry, jestem asystentem głosowym. Mam się dobrze, dziękuję."


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = words(reference), words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / len(ref)


async def run(cfg: Config, text: str, max_wer: float, stt: STTEngine | None, tts: TTSEngine | None) -> int:
    stt = stt or build_stt(cfg.brain.stt)
    tts = tts or build_tts(cfg.brain.tts)
    if stt is None or tts is None:
        print("W konfiguracji muszą być włączone brain.stt i brain.tts.", file=sys.stderr)
        return 2
    audio = cfg.brain.audio
    try:
        t0 = time.monotonic()
        speech = await tts.synthesize(text)
        tts_s = time.monotonic() - t0
        seconds = len(speech.pcm) / 2 / speech.rate
        print(f"TTS: {seconds:.1f} s mowy w {tts_s:.1f} s (x{seconds / max(tts_s, 1e-9):.1f} czasu rzeczywistego), {speech.rate} Hz")

        # tor mikrofonu ciała: 16 kHz, pakiety Opus, składanie wypowiedzi
        mic = resample(np.frombuffer(speech.pcm, dtype=np.int16), speech.rate, STT_RATE)
        packets = OpusEncoder(STT_RATE, audio.frame_ms, audio.bitrate_bps).encode(mic)
        assembler = UtteranceAssembler(audio)
        assembler.start(1)
        for seq, packet in enumerate(packets):
            assembler.add(AudioFrame(1, 1, seq, packet))
        raw = assembler.take(1)
        utterance = None if raw is None else assembler.decode(raw)
        if utterance is None:
            print("Wypowiedź pominięta (za krótka lub pusta).", file=sys.stderr)
            return 1
        print(f"Opus: {len(packets)} pakietów, {utterance.duration_ms:.0f} ms po złożeniu")

        t0 = time.monotonic()
        transcript = await stt.transcribe(utterance.pcm, STT_RATE)
        stt_s = time.monotonic() - t0
        wer = word_error_rate(text, transcript.text)
        print(f"STT: {stt_s:.1f} s (pierwsze wywołanie obejmuje ładowanie modelu)")
        print(f"Oryginał:   {text}")
        print(f"Rozpoznane: {transcript.text}")
        print(f"Błędne słowa: {wer:.0%} (próg {max_wer:.0%})")
        return 0 if wer <= max_wer else 1
    except SpeechError as e:
        print(f"Błąd: {e}", file=sys.stderr)
        return 2
    finally:
        await stt.aclose()
        await tts.aclose()


def main(argv: list[str] | None = None, *, stt: STTEngine | None = None, tts: TTSEngine | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m host.brain.selftest", description=__doc__.split("\n")[0])
    p.add_argument("--config", default=os.environ.get("IRIS_CONFIG"), help="plik TOML (domyślnie IRIS_CONFIG)")
    p.add_argument("--text", default=DEFAULT_TEXT)
    p.add_argument("--max-wer", type=float, default=0.34, help="próg odsetka błędnych słów")
    args = p.parse_args(argv)
    cfg = Config.load(args.config) if args.config else Config()
    return asyncio.run(run(cfg, args.text, args.max_wer, stt, tts))


if __name__ == "__main__":
    sys.exit(main())
