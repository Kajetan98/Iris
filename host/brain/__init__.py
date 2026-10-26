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
from host.core.config import LLMConfig

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
    "Usage",
    "build_assistant",
    "build_engine",
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
