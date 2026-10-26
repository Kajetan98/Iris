"""Interfejsy silników mowy (docs/spec.md, sekcja 6). Silniki są lokalne i wymienne."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    duration_s: float
    avg_logprob: float | None = None


@dataclass(frozen=True)
class Speech:
    pcm: bytes  # s16le, mono
    rate: int


class SpeechError(Exception):
    """Błąd rozpoznawania lub syntezy mowy."""


class SpeechConfigError(SpeechError):
    """Brak modelu lub głosu, zła konfiguracja. Ponowienie nie pomoże."""


class STTEngine(Protocol):
    async def transcribe(self, pcm: bytes, rate: int) -> Transcript:
        """pcm: s16le mono."""
        ...

    async def aclose(self) -> None: ...


class TTSEngine(Protocol):
    async def synthesize(self, text: str) -> Speech: ...

    async def aclose(self) -> None: ...
