"""Pomocniki testów host/brain."""

from datetime import datetime, timezone

from host.brain.llm import LLMReply, Usage

T0 = datetime(2026, 10, 28, 14, 5, tzinfo=timezone.utc)


class ScriptedEngine:
    """Silnik z gotowymi odpowiedziami; zapisuje wywołania."""

    def __init__(self, replies=("Dobrze.",), *, remote=False):
        self._replies = list(replies)
        self.remote = remote
        self.calls = []
        self.closed = False

    async def complete(self, system, messages, *, max_tokens):
        self.calls.append((system, list(messages), max_tokens))
        item = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, LLMReply):
            return item
        return LLMReply(item, "stop", "scripted", Usage(10, 5))

    @property
    def chat_calls(self):
        """Tylko zapytania rozmowy, bez zapytań o pojęcia z wypowiedzi (host/brain/learn.py)."""
        from host.brain.learn import CONCEPTS_PROMPT

        return [c for c in self.calls if c[0] != CONCEPTS_PROMPT]

    async def aclose(self):
        self.closed = True

