"""Synteza mowy: Piper, lokalnie (docs/spec.md, sekcja 6).

Uwaga: pakiet piper-tts ma licencję GPL-3.0-or-later.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from host.brain.speech import Speech, SpeechConfigError, SpeechError

log = logging.getLogger(__name__)

MAX_ERROR_TEXT = 200


class PiperEngine:
    def __init__(
        self,
        voice: str,
        *,
        length_scale: float | None = None,
        speaker_id: int | None = None,
        volume: float | None = None,
        max_chars: int = 1000,
        voice_loader: Callable[[str], Any] | None = None,
        synthesis_config_cls: type | None = None,
    ) -> None:
        self.voice_path = voice
        self.max_chars = max_chars
        self._syn_args = {
            k: v
            for k, v in {"length_scale": length_scale, "speaker_id": speaker_id, "volume": volume}.items()
            if v is not None
        }
        self._loader = voice_loader
        self._syn_cls = synthesis_config_cls
        self._voice: Any = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> Any:
        if self._voice is not None:
            return self._voice
        path = Path(self.voice_path)
        if not path.is_file():
            raise SpeechConfigError(f"plik głosu Piper nie istnieje: {self.voice_path}")
        config = Path(str(path) + ".json")
        if self._loader is None and not config.is_file():
            raise SpeechConfigError(f"brak pliku konfiguracji głosu: {config}")
        loader = self._loader or _piper_loader()
        try:
            self._voice = loader(str(path))
        except (OSError, ValueError, RuntimeError) as e:
            raise SpeechConfigError(f"nie można załadować głosu Piper: {str(e)[:MAX_ERROR_TEXT]}") from None
        log.info("głos TTS załadowany: %s", path.name)
        return self._voice

    async def synthesize(self, text: str) -> Speech:
        return await asyncio.to_thread(self._synthesize, text)

    def _synthesize(self, text: str) -> Speech:
        text = text.strip()
        if not text:
            raise SpeechError("pusty tekst do syntezy")
        if len(text) > self.max_chars:
            log.warning("tekst do syntezy ucięty z %d do %d znaków", len(text), self.max_chars)
            text = text[: self.max_chars]
        with self._lock:
            voice = self._load_locked()
            try:
                syn_config = self._syn_config()
                chunks = list(voice.synthesize(text, syn_config) if syn_config else voice.synthesize(text))
            except SpeechError:
                raise
            except Exception as e:
                raise SpeechError(f"błąd syntezy mowy: {type(e).__name__}") from None
        if not chunks:
            raise SpeechError("synteza nie zwróciła dźwięku")
        rate = chunks[0].sample_rate
        if any(c.sample_rate != rate or c.sample_channels != 1 or c.sample_width != 2 for c in chunks):
            raise SpeechError("głos Piper zwrócił dźwięk w nieobsługiwanym formacie")
        return Speech(b"".join(c.audio_int16_bytes for c in chunks), rate)

    def _syn_config(self) -> Any:
        if not self._syn_args:
            return None
        cls = self._syn_cls
        if cls is None:
            from piper import SynthesisConfig as cls  # noqa: N813
        return cls(**self._syn_args)

    async def aclose(self) -> None:
        self._voice = None


def _piper_loader() -> Callable[[str], Any]:
    try:
        from piper import PiperVoice
    except ImportError as e:
        raise SpeechConfigError("brak biblioteki piper-tts (pip install piper-tts)") from e
    return PiperVoice.load
