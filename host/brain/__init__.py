"""LLM, STT i TTS. Cały moduł można wyłączyć bez wpływu na resztę hosta."""

from __future__ import annotations

from host.brain.assistant import Answer, Assistant
from host.brain.llm import (
    LLMConfigError,
    LLMEngine,
    LLMError,
    LLMRefusal,
    LLMReply,
    LLMUnavailable,
    Message,
    Usage,
)
from host.brain.speech import (
    Speech,
    SpeechConfigError,
    SpeechError,
    STTEngine,
    Transcript,
    TTSEngine,
)
from host.core.config import LLMConfig, STTConfig, TTSConfig

__all__ = [
    "Answer",
    "Assistant",
    "LLMConfigError",
    "LLMEngine",
    "LLMError",
    "LLMRefusal",
    "LLMReply",
    "LLMUnavailable",
    "Message",
    "STTEngine",
    "Speech",
    "SpeechConfigError",
    "SpeechError",
    "TTSEngine",
    "Transcript",
    "Usage",
    "build_assistant",
    "build_engine",
    "build_stt",
    "build_tts",
]


def build_engine(cfg: LLMConfig) -> LLMEngine | None:
    """Silnik z konfiguracji albo None, gdy LLM jest wyłączony."""
    if cfg.engine == "none":
        return None
    if cfg.engine == "ollama":
        from host.brain.ollama import OllamaEngine

        assert cfg.model is not None  # gwarantuje walidator LLMConfig
        return OllamaEngine(
            cfg.model,
            base_url=cfg.base_url,
            timeout_s=cfg.timeout_s,
            retries=cfg.retries,
            temperature=cfg.temperature,
        )
    raise LLMConfigError(f"nieznany silnik LLM: {cfg.engine!r}")


def build_assistant(cfg: LLMConfig) -> Assistant | None:
    engine = build_engine(cfg)
    return None if engine is None else Assistant(engine, cfg)


def build_stt(cfg: STTConfig) -> STTEngine | None:
    """Silnik STT z konfiguracji albo None, gdy wyłączony. Model ładuje się przy pierwszym użyciu."""
    if cfg.engine == "none":
        return None
    from host.brain.stt_whisper import FasterWhisperEngine

    assert cfg.model is not None  # gwarantuje walidator STTConfig
    return FasterWhisperEngine(
        cfg.model,
        language=cfg.language,
        device=cfg.device,
        compute_type=cfg.compute_type,
        beam_size=cfg.beam_size,
        vad_filter=cfg.vad_filter,
        model_dir=cfg.model_dir,
        local_files_only=cfg.local_files_only,
        cpu_threads=cfg.cpu_threads,
    )


def build_tts(cfg: TTSConfig) -> TTSEngine | None:
    """Silnik TTS z konfiguracji albo None, gdy wyłączony."""
    if cfg.engine == "none":
        return None
    from host.brain.tts_piper import PiperEngine

    assert cfg.voice is not None  # gwarantuje walidator TTSConfig
    return PiperEngine(
        cfg.voice,
        length_scale=cfg.length_scale,
        speaker_id=cfg.speaker_id,
        volume=cfg.volume,
        max_chars=cfg.max_chars,
    )
