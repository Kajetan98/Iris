"""Silnik Claude API przez oficjalne SDK (docs/spec.md, sekcja 12).

Dane trafiają do chmury Anthropic, więc silnik jest zdalny. Klucz API pochodzi ze zmiennych
środowiskowych SDK i nie jest nigdzie zapisywany ani logowany; treść rozmowy też nie.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import anthropic

from host.brain.llm import (
    FinishReason,
    LLMConfigError,
    LLMError,
    LLMReply,
    LLMUnavailable,
    Message,
    Usage,
)

log = logging.getLogger(__name__)


def _keep_conversation_out_of_sdk_logs() -> None:
    """Logger SDK na poziomie DEBUG (np. ANTHROPIC_LOG=debug albo DEBUG na root) wypisuje całe
    żądanie z promptem i rozmową. Ograniczamy go do INFO, bo treść rozmowy nie może trafiać do logów."""
    sdk_logger = logging.getLogger("anthropic")
    if sdk_logger.getEffectiveLevel() < logging.INFO:
        sdk_logger.setLevel(logging.INFO)


_keep_conversation_out_of_sdk_logs()

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MAX_ERROR_TEXT = 200

# Modele z serwerowym zapasem na odmowę klasyfikatora. Dla pozostałych (np. Haiku) parametr
# `fallbacks` nie istnieje albo kończy się błędem 400.
FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_FINISH: dict[str | None, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "refusal": "refusal",
}


class ClaudeEngine:
    def __init__(
        self,
        model: str = "claude-opus-5-5",
        *,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        retries: int = 2,
        effort: str = "low",
        fallbacks: bool = True,
        thinking_headroom_tokens: int = 2000,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url
        self._timeout = timeout_s
        self._retries = retries
        self._effort = effort
        self._headroom = thinking_headroom_tokens
        self._use_fallbacks = fallbacks and model in FALLBACK_MODELS
        self._client = client
        self._owns_client = client is None

    @property
    def remote(self) -> bool:
        if self._base_url is None:
            return True
        return (urlparse(self._base_url).hostname or "") not in LOOPBACK_HOSTS

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            _keep_conversation_out_of_sdk_logs()
            # Klucz i profil rozwiązuje SDK; brak poświadczeń wychodzi dopiero przy żądaniu.
            self._client = anthropic.AsyncAnthropic(
                base_url=self._base_url, timeout=self._timeout, max_retries=self._retries
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.close()
            self._client = None

    async def complete(
        self, system: str, messages: Sequence[Message], *, max_tokens: int
    ) -> LLMReply:
        # Tokeny myślenia liczą się do max_tokens, więc odpowiedź dostaje zapas ponad swój limit.
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens + self._headroom,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if self._effort != "off":
            request["output_config"] = {"effort": self._effort}
        client = self._get_client()
        try:
            if self._use_fallbacks:
                response = await client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **request
                )
            else:
                response = await client.messages.create(**request)
        except TypeError as e:
            if "authentication method" in str(e):
                raise LLMConfigError(
                    "brak poświadczeń Claude API (ustaw zmienną środowiskową ANTHROPIC_API_KEY)"
                ) from None
            raise
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
            raise LLMConfigError(f"Claude API zwróciło {e.status_code}: {_message(e)}") from None
        except (
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
        ):
            raise LLMUnavailable(f"brak połączenia z Claude API lub limit czasu {self._timeout:g} s") from None
        except (
            anthropic.RateLimitError,
            anthropic.OverloadedError,
            anthropic.InternalServerError,
            anthropic.DeadlineExceededError,
        ) as e:
            raise LLMUnavailable(f"Claude API zwróciło {e.status_code}: {_message(e)}") from None
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise LLMUnavailable(f"Claude API zwróciło {e.status_code}: {_message(e)}") from None
            raise LLMError(f"Claude API zwróciło {e.status_code}: {_message(e)}") from None
        except anthropic.AnthropicError as e:
            raise LLMError(f"błąd SDK Claude: {type(e).__name__}") from None
        return self._parse(response)

    def _parse(self, response: Any) -> LLMReply:
        finish = _FINISH.get(response.stop_reason, "other")
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        usage = response.usage
        input_tokens = (
            (usage.input_tokens or 0)
            + (usage.cache_creation_input_tokens or 0)
            + (usage.cache_read_input_tokens or 0)
        )
        details = getattr(response, "stop_details", None)
        log.debug(
            "claude: request_id=%s model=%s stop=%s%s",
            getattr(response, "_request_id", None), response.model, response.stop_reason,
            f" category={details.category}" if finish == "refusal" and details else "",
        )
        if response.model != self.model:
            log.info("odpowiedź z modelu zapasowego: %s zamiast %s", response.model, self.model)
        return LLMReply(
            text=text,
            finish=finish,
            model=response.model,
            usage=Usage(input_tokens, usage.output_tokens),
        )


def _message(e: anthropic.APIStatusError) -> str:
    return (e.message or "brak opisu")[:MAX_ERROR_TEXT]
