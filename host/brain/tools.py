"""Narzędzia pamięci dla modelu (docs/spec.md, sekcja 12, "Narzędzia pamięci").

Tu są tylko definicje i interfejs. Wykonanie (odczyt i zapis grafu) należy do host/api, które zna bazę.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from host.brain.llm import ToolCall, ToolResult, ToolSpec
from host.core.config import MEMORY_DOMAIN_IDS

RECALL = ToolSpec(
    name="iris_recall",
    description=(
        "Przypomnij sobie, co pamięć IRIS kojarzy z tematem: pasujące pojęcia z wcześniejszych rozmów i pojęcia "
        "z nimi powiązane, od najsilniejszych. Użyj, gdy rozmowa dotyczy czegoś, o czym użytkownik mógł już "
        "wcześniej mówić, albo gdy pyta, co pamiętasz na dany temat. Wynik to pojęcia, nie zdania."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Temat albo zdanie, np. 'pies' lub 'lampa w salonie'."}},
        "required": ["query"],
        "additionalProperties": False,
    },
)

REMEMBER = ToolSpec(
    name="iris_remember",
    description=(
        "Zapisz w pamięci IRIS pojęcia z jednego zdania, tylko gdy użytkownik wprost prosi o zapamiętanie "
        "czegoś. Rozmowy uczą pamięć same, więc nie używaj tego dla zwykłych wypowiedzi. Liczby i dane wrażliwe "
        "nie są zapisywane."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Co zapamiętać, jednym zdaniem."},
            "domain": {"type": "string", "enum": list(MEMORY_DOMAIN_IDS),
                       "description": "Opcjonalnie: dom, czas, ty albo rozmowa."},
        },
        "required": ["text"],
        "additionalProperties": False,
    },
)


class ToolBox(Protocol):
    """Narzędzia jednej tury rozmowy. Tworzy je warstwa api dla każdej wypowiedzi."""

    def specs(self, *, remote: bool) -> Sequence[ToolSpec]:
        """Narzędzia, które wolno dać temu silnikowi (polityka `send_memory`)."""
        ...

    async def call(self, call: ToolCall) -> ToolResult:
        """Wykonanie narzędzia. Błąd zwraca jako wynik z `is_error`, nie jako wyjątek."""
        ...

    async def context(self, text: str, *, remote: bool) -> Sequence[str]:
        """Pojęcia do bloku kontekstu, gdy model nie dostaje `iris_recall` (puste, gdy pamięć nie może
        trafić do tego silnika)."""
        ...
