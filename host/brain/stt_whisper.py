"""Rozpoznawanie mowy: faster-whisper, lokalnie (docs/spec.md, sekcja 6)."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

from host.brain.audio import STT_RATE, resample
from host.brain.speech import SpeechConfigError, SpeechError, Transcript

log = logging.getLogger(__name__)

MAX_ERROR_TEXT = 200


class FasterWhisperEngine:
    def __init__(
        self,
        model: str,
        *,
        language: str = "pl",
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 1,
        vad_filter: bool = True,
        model_dir: str | None = None,
        local_files_only: bool = False,
        cpu_threads: int = 0,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model_name = model
        self._language = language
        self._beam_size = beam_size
        self._vad_filter = vad_filter
        self._load_args: dict[str, Any] = {
            "device": device,
            "compute_type": compute_type,
            "download_root": model_dir,
            "local_files_only": local_files_only,
            "cpu_threads": cpu_threads,
        }
        self._factory = model_factory
        self._model: Any = None
        self._lock = threading.Lock()  # model ładujemy i używamy po jednym wątku

    def load(self) -> None:
        """Ładuje model (idempotentnie). Blokuje, więc z pętli zdarzeń wołaj przez wątek."""
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> Any:
        if self._model is not None:
            return self._model
        looks_like_path = os.sep in self.model_name or self.model_name.startswith(".")
        if looks_like_path and not os.path.isdir(self.model_name):
            raise SpeechConfigError(f"katalog modelu STT nie istnieje: {self.model_name}")
        factory = self._factory or _whisper_model_class()
        try:
            self._model = factory(self.model_name, **self._load_args)
        except (OSError, ValueError, RuntimeError) as e:
            raise SpeechConfigError(
                f"nie można załadować modelu STT {self.model_name!r}: "
                f"{str(e)[:MAX_ERROR_TEXT]} (nazwa modelu jest pobierana z Hugging Face, "
                "więc host huggingface.co musi być dostępny albo trzeba podać katalog z modelem)"
            ) from None
        log.info("model STT załadowany: %s", self.model_name)
        return self._model

    async def transcribe(self, pcm: bytes, rate: int) -> Transcript:
        return await asyncio.to_thread(self._transcribe, pcm, rate)

    def _transcribe(self, pcm: bytes, rate: int) -> Transcript:
        samples = np.frombuffer(pcm, dtype=np.int16)
        samples = resample(samples, rate, STT_RATE)
        audio = samples.astype(np.float32) / 32768.0
        with self._lock:
            model = self._load_locked()
            try:
                segments, info = model.transcribe(
                    audio,
                    language=self._language,
                    beam_size=self._beam_size,
                    vad_filter=self._vad_filter,
                    condition_on_previous_text=False,
                )
                parts = list(segments)  # transkrypcja dzieje się przy iteracji
            except Exception as e:
                raise SpeechError(f"błąd rozpoznawania mowy: {type(e).__name__}") from None
        text = " ".join(s.text.strip() for s in parts if s.text.strip())
        logprobs = [s.avg_logprob for s in parts if getattr(s, "avg_logprob", None) is not None]
        return Transcript(
            text=text,
            language=getattr(info, "language", None),
            duration_s=audio.size / STT_RATE,
            avg_logprob=sum(logprobs) / len(logprobs) if logprobs else None,
        )

    async def aclose(self) -> None:
        self._model = None


def _whisper_model_class() -> Callable[..., Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise SpeechConfigError("brak biblioteki faster-whisper (pip install faster-whisper)") from e
    return WhisperModel
