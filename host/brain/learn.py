"""Pojęcia z wypowiedzi użytkownika (docs/spec.md, sekcja 9, "Nauka z rozmowy").

LLM tylko proponuje: osobne zapytanie z samą wypowiedzią, bez historii, stanu i pamięci, odpowiedź
jako obiekt JSON. Co trafi do grafu, rozstrzygają reguły z host/core/memory.py.
"""

from __future__ import annotations

from host.brain.llm import LLMEngine, LLMError, Message
from host.core.config import MemoryConfig
from host.core.memory import Extraction, parse_llm

MAX_TOKENS = 300


class LearnError(Exception):
    """Silnik nie podał poprawnej odpowiedzi. Host używa wtedy heurystyki."""


CONCEPTS_PROMPT = """\
Wyciągasz pojęcia do pamięci asystenta głosowego IRIS z jednej wypowiedzi użytkownika.

Zasady:
- Do 8 pojęć: rzeczowniki, nazwy własne albo czasowniki niosące treść, w formie podstawowej (mianownik liczby pojedynczej, bezokolicznik), małymi literami poza nazwami własnymi i skrótami.
- Pomijasz słowa funkcyjne, grzeczności i liczby.
- Pomijasz zdrowie, leki, położenie i adres, pieniądze, wyznanie, poglądy polityczne i orientację.
- Domena: dom = dom i sterowanie urządzeniami, czas = przypomnienia, kalendarz, plany, ty = użytkownik i jego bliscy, upodobania, praca, zajęcia, rozmowa = wszystko inne.
- Odpowiadasz wyłącznie obiektem JSON, bez innego tekstu: {"domain": "dom|czas|ty|rozmowa", "concepts": ["...", "..."]}. Gdy nie ma nic do zapamiętania: {"domain": "rozmowa", "concepts": []}.
"""


async def extract_concepts(engine: LLMEngine, utterance: str, cfg: MemoryConfig) -> Extraction:
    """Pojęcia z jednej wypowiedzi (spec, sekcja 9). Rzuca LLMError albo LearnError; wtedy host używa
    heurystyki. Filtr liczb i danych wrażliwych stosuje wywołujący (host/core/memory.keep)."""
    reply = await engine.complete(CONCEPTS_PROMPT, [Message("user", utterance)], max_tokens=MAX_TOKENS)
    if reply.finish == "refusal":
        raise LearnError("silnik odmówił")
    extraction = parse_llm(reply.text, cfg)
    if extraction is None:
        raise LearnError("odpowiedź bez poprawnego obiektu JSON")
    return extraction


__all__ = ["CONCEPTS_PROMPT", "LLMError", "LearnError", "extract_concepts"]
