"""Wnioski o użytkowniku z jego wypowiedzi (docs/spec.md, sekcja 9, "Uczenie się").

LLM tylko proponuje: osobne zapytanie z samą wypowiedzią, bez historii, stanu i znanych faktów,
odpowiedź jako lista JSON. Co trafi do pamięci, rozstrzygają reguły z host/core/learning.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from host.brain.llm import LLMEngine, LLMError, Message
from host.core.learning import MAX_FACT_CHARS, MIN_FACT_CHARS, fact_text

EXTRACT_PROMPT = """\
Z wypowiedzi użytkownika wypisujesz trwałe fakty o nim samym, które przydadzą się asystentowi głosowemu w kolejnych rozmowach: zainteresowania, zwyczaje, plany, praca, bliskie osoby i zwierzęta, upodobania, jak chce, żeby się do niego zwracać.

Zasady:
- Tylko to, co wynika z wypowiedzi. Nie dopowiadasz niczego ponad nią.
- Pomijasz pytania, polecenia i rzeczy chwilowe (pogoda, samopoczucie w tej chwili).
- Pomijasz zdrowie, leki, położenie i adres, pieniądze, wyznanie, poglądy polityczne i orientację.
- Każdy fakt to jedno krótkie zdanie w drugiej osobie liczby pojedynczej, np. "Masz psa o imieniu Burek."
- Pewność od 0 do 1 mówi, jak wyraźnie wypowiedź potwierdza fakt.
- Odpowiadasz wyłącznie listą JSON, bez innego tekstu: [{"fakt": "...", "pewnosc": 0.7}]. Gdy nie ma faktów: [].
"""
MAX_TOKENS = 300


class LearnError(Exception):
    """Silnik nie podał poprawnej listy wniosków. Tura rozmowy się nie zmienia."""


@dataclass(frozen=True)
class Guess:
    text: str
    confidence: float


def parse_guesses(raw: str, *, limit: int) -> list[Guess]:
    """Lista wniosków z odpowiedzi silnika (także w bloku ```json). LearnError, gdy to nie lista."""
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < start:
        raise LearnError("odpowiedź bez listy JSON")
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        raise LearnError("niepoprawny JSON") from None
    if not isinstance(data, list):
        raise LearnError("odpowiedź nie jest listą")
    guesses: list[Guess] = []
    for item in data:
        if len(guesses) >= limit:
            break
        if not isinstance(item, dict):
            continue
        text = item.get("fakt", item.get("text"))
        conf = item.get("pewnosc", item.get("pewność", item.get("confidence")))
        if not isinstance(text, str) or isinstance(conf, bool) or not isinstance(conf, (int, float)):
            continue
        text = fact_text(text)
        if MIN_FACT_CHARS <= len(text) <= MAX_FACT_CHARS:
            guesses.append(Guess(text, min(1.0, max(0.0, float(conf)))))
    return guesses


async def infer_facts(engine: LLMEngine, utterance: str, *, limit: int) -> list[Guess]:
    """Propozycje faktów z jednej wypowiedzi. Rzuca LLMError albo LearnError."""
    reply = await engine.complete(EXTRACT_PROMPT, [Message("user", utterance)], max_tokens=MAX_TOKENS)
    if reply.finish == "refusal":
        raise LearnError("silnik odmówił")
    return parse_guesses(reply.text, limit=limit)


__all__ = ["EXTRACT_PROMPT", "Guess", "LLMError", "LearnError", "infer_facts", "parse_guesses"]
