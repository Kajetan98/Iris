"""Uczenie się faktów o użytkowniku (docs/spec.md, sekcja 9, "Uczenie się").

Deterministyczne reguły bez modelu językowego: polecenia pamięci, odpowiedzi tak/nie, podobieństwo
tekstów, wybór pytania i tekst odpowiedzi na "co o mnie wiesz". LLM tylko proponuje wnioski
(host/brain/learn.py), a o tym, co z nimi zrobić, decydują reguły z tego modułu.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from host.core.config import LearningConfig
from host.store.models import Fact

MIN_FACT_CHARS = 3
MAX_FACT_CHARS = 300

_PREFIX = r"^(?:iris[\s,]+)?"
_REMEMBER = re.compile(
    _PREFIX + r"(?:(?:zapamiętaj|zapisz)(?:\s+sobie)?[\s,:]+(?:(?:o\s+tym,?\s+)?że\s+)?"
    r"|pamiętaj(?:\s+sobie)?[\s,:]+(?:o\s+tym,?\s+)?że\s+)(?P<x>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_FORGET_ALL = re.compile(_PREFIX + r"zapomnij\s+(?:o\s+mnie\s+)?wszystko(?:\s+o\s+mnie)?$", re.IGNORECASE)
_FORGET = re.compile(
    _PREFIX + r"zapomnij[\s,:]+(?:(?:o\s+tym,?\s+)?że\s+|o\s+)?(?P<x>.+)$", re.IGNORECASE | re.DOTALL
)
_RECALL = re.compile(
    _PREFIX + r"(?:co\s+(?:o\s+mnie\s+wiesz|wiesz\s+o\s+mnie)|czego\s+się\s+o\s+mnie\s+nauczył(?:aś|eś))$",
    re.IGNORECASE,
)
_YES = re.compile(r"^(?:tak|zgadza\s+się|dokładnie|owszem)\b", re.IGNORECASE)
_NO = re.compile(r"^(?:nie\s+zgadza\s+się|nieprawda|nie)\b[\s,.:;!-]*(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)
# Słowa pomocnicze pomijane przy porównaniu (zaimki, "mam/masz", "jest/jesteś").
_STOPWORDS = {
    "jest", "jestem", "jesteś", "mam", "masz", "się", "nie", "tak", "oraz", "który", "która", "które",
    "tego", "tym", "ten", "jak", "dla", "przez", "bardzo", "mój", "moja", "moje", "twój", "twoja",
    "twoje", "mnie", "ciebie", "też", "także", "lub", "albo",
}


@dataclass(frozen=True)
class MemoryCommand:
    kind: Literal["remember", "forget", "forget_all", "recall"]
    text: str = ""


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    return text.rstrip(".!?…").strip()


def fact_text(text: str) -> str:
    """Tekst faktu w słowach użytkownika: bez cudzysłowów i końcowej interpunkcji."""
    return _clean(text).strip("\"'„”«» ").strip()


def parse_memory(text: str) -> MemoryCommand | None:
    """Polecenie pamięci albo None. Tekst faktu zachowuje wielkość liter wypowiedzi."""
    t = _clean(text)
    t = t.rstrip("?").strip()
    if _RECALL.match(t):
        return MemoryCommand("recall")
    if _FORGET_ALL.match(t):
        return MemoryCommand("forget_all")
    for kind, pattern in (("remember", _REMEMBER), ("forget", _FORGET)):
        m = pattern.match(t)
        if m:
            x = fact_text(m.group("x"))
            if len(x) >= MIN_FACT_CHARS:
                return MemoryCommand(kind, x[:MAX_FACT_CHARS])
            return None
    return None


def parse_answer(text: str) -> tuple[bool | None, str]:
    """Odpowiedź na pytanie: (True, "") dla "tak", (False, poprawka) dla "nie", (None, "") inaczej."""
    t = _clean(text)
    if _YES.match(t):
        return True, ""
    m = _NO.match(t)
    if m:
        rest = fact_text(m.group("rest"))
        return False, rest if len(rest) >= MIN_FACT_CHARS else ""
    return None, ""


def _key(word: str) -> str:
    # Rdzeń przybliżony: polska odmiana zmienia końcówki ("pracuję"/"pracujesz", "kawa"/"kawę").
    return word[:4] if len(word) > 4 else word[:3]


def stems(text: str) -> set[str]:
    words = re.findall(r"\w+", text.lower())
    return {_key(w) for w in words if len(w) >= 3 and not w.isdigit() and w not in _STOPWORDS} | {
        w for w in words if w.isdigit()
    }


def similarity(a: str, b: str) -> float:
    """Indeks Jaccarda zbiorów rdzeni słów (spec, sekcja 9)."""
    sa, sb = stems(a), stems(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def best_match(text: str, facts: Iterable[Fact], min_score: float) -> Fact | None:
    """Najbardziej podobny fakt (przy remisie nowszy) albo None poniżej progu."""
    best: tuple[float, float, Fact] | None = None
    for f in facts:
        score = similarity(text, f.text)
        key = (score, f.updated_at.timestamp(), f)
        if score >= min_score and (best is None or key[:2] > best[:2]):
            best = key
    return None if best is None else best[2]


def is_sensitive(text: str, stems_: Sequence[str]) -> bool:
    """Czy tekst zawiera rdzeń z listy danych wrażliwych (dopasowanie do początku słowa)."""
    words = re.findall(r"\w+", text.lower())
    return any(w.startswith(s) for w in words for s in stems_)


def question_for(fact: Fact) -> str:
    t = fact_text(fact.text)
    if t[:1].isupper() and not t[1:2].isupper():
        t = t[0].lower() + t[1:]
    return f"Czy dobrze rozumiem: {t}?"


def pick_question(facts: Iterable[Fact], now: datetime, cfg: LearningConfig) -> Fact | None:
    """Przypuszczenie do zapytania: najwyższa pewność, przy remisie najnowsze."""
    again = timedelta(days=cfg.ask_again_days)
    candidates = [
        f for f in facts
        if f.source == "inferred" and f.confidence >= cfg.ask_min
        and (f.asked_at is None or now - f.asked_at >= again)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.confidence, f.updated_at.timestamp(), f.id))


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def recall_text(facts: Sequence[Fact], min_inferred: float) -> str:
    """Odpowiedź na "co o mnie wiesz": 5 faktów podanych wprost i 3 przypuszczenia."""
    stated = sorted((f for f in facts if f.source == "stated"), key=lambda f: -f.updated_at.timestamp())
    guesses = sorted(
        (f for f in facts if f.source == "inferred" and f.confidence >= min_inferred),
        key=lambda f: (-f.confidence, -f.updated_at.timestamp()),
    )
    if not facts:
        return "Jeszcze nic o tobie nie wiem. Możesz powiedzieć: zapamiętaj, że ..."
    parts = []
    shown = stated[:5]
    if shown:
        parts.append("Wiem od ciebie: " + "; ".join(f'"{fact_text(f.text)}"' for f in shown) + ".")
    shown_guesses = guesses[:3]
    if shown_guesses:
        items = []
        for f in shown_guesses:
            t = fact_text(f.text)
            t = t[0].lower() + t[1:] if t[:1].isupper() and not t[1:2].isupper() else t
            items.append(f"{t} ({round(f.confidence * 100)}%)")
        parts.append("Przypuszczam, że " + "; ".join(items) + ".")
    rest = len(facts) - len(shown) - len(shown_guesses)
    if not parts:
        word = _plural(rest, "niepewne przypuszczenie", "niepewne przypuszczenia", "niepewnych przypuszczeń")
        return f"Mam tylko {rest} {word}, pełna lista jest w aplikacji."
    if rest > 0:
        word = _plural(rest, "informacja", "informacje", "informacji")
        parts.append(f"Oprócz tego {rest} {word}, pełna lista jest w aplikacji.")
    return " ".join(parts)
