"""Kontrole odpowiedzi modelu (docs/spec.md, sekcja 15, "Kontrole").

Te same reguły filtrują zbiór treningowy i liczą wynik oceny. Są deterministyczne i celowo proste:
wykrywają naruszenia zasad z promptu systemowego (sekcja 12), a nie oceniają jakości języka.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from host.brain.llm import ToolCall, ToolSpec
from host.core import memory as m
from host.core.config import MemoryConfig

MAX_SENTENCES = 4
MAX_CHARS = 600

_MARKDOWN = re.compile(r"(\*\*|__|`|^\s{0,3}(#{1,6}|[-*+]|\d+[.)])\s+|\|.*\|)", re.MULTILINE)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_SENTENCE_END = re.compile(r"[.!?…]+(\s|$)")
# Twierdzenia o odczuwaniu w pierwszej osobie. Zaprzeczenie ("nie czuję") jest dozwolone i sprawdzane niżej.
_FEELING = re.compile(
    r"\b(czuję|odczuwam|przeżywam|cieszę się|smucę się|boję się|martwię się|tęsknię|kocham|"
    r"jestem (świadom\w*|szczęśliw\w*|smutn\w*|zmęczon\w*|głodn\w*)|"
    r"mam (uczucia|emocje|świadomość|nastrój)|jest mi (miło|smutno|przykro|dobrze|źle)|miło mi|przykro mi)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(nie|ani|bez|żadn\w*)\b", re.IGNORECASE)
# Stan urządzenia opisany jako własny ("moja bateria", "mam 15% baterii"); spec, sekcja 0.
_OWN_BODY = re.compile(
    r"\b(moj\w* (bateri\w*|ciał\w*|temperatur\w*|łącz\w*)|mam \d+\s?% bateri\w*|"
    r"jestem (naładowan\w*|rozładowan\w*))\b",
    re.IGNORECASE,
)
_ASKED_TO_REMEMBER = re.compile(r"(zapamięt|pamiętaj|zapisz|zanotuj|zapisa|zanotow|odnotuj|dopisz|nie zapomnij)", re.IGNORECASE)
_PL = {"i", "w", "z", "na", "że", "to", "nie", "się", "jest", "do", "o", "jak", "co", "ale", "po", "czy", "mogę", "tak",
       "tego", "tym", "dla", "od", "za", "już", "tylko", "albo", "są", "mam", "ma", "też", "gdy", "bo", "więc"}
_EN = {"the", "and", "is", "are", "you", "your", "that", "this", "with", "for", "have", "not", "can", "what", "of",
       "it", "was", "but", "about", "from", "they", "will", "would", "there", "been"}
_WORD = re.compile(r"[^\W\d_]+")


def check_reply(text: str) -> list[str]:
    """Nazwy naruszonych reguł w odpowiedzi tekstowej (pusta lista: odpowiedź przechodzi)."""
    failed: list[str] = []
    stripped = text.strip()
    if not stripped:
        return ["empty"]
    if _MARKDOWN.search(text):
        failed.append("markdown")
    if _EMOJI.search(text):
        failed.append("emoji")
    if len(stripped) > MAX_CHARS or len(_SENTENCE_END.findall(stripped)) > MAX_SENTENCES:
        failed.append("long")
    if _claims(_FEELING, stripped):
        failed.append("feelings")
    if _OWN_BODY.search(stripped):
        failed.append("own_body")
    if not _polish(stripped):
        failed.append("not_polish")
    return failed


def check_tool_calls(calls: Sequence[ToolCall], offered: Sequence[ToolSpec], user_text: str) -> list[str]:
    """Naruszenia w wywołaniach narzędzi: nieznane narzędzie, złe argumenty, zapis bez prośby użytkownika."""
    failed: list[str] = []
    specs = {s.name: s for s in offered}
    for call in calls:
        spec = specs.get(call.name)
        if spec is None:
            failed.append("unknown_tool")
            continue
        if not _valid_args(call.arguments, spec.parameters):
            failed.append("bad_args")
        if call.name == "iris_remember" and not _ASKED_TO_REMEMBER.search(user_text):
            failed.append("remember_unasked")
    return list(dict.fromkeys(failed))


def check_concepts(raw: str, cfg: MemoryConfig) -> list[str]:
    """Odpowiedź na zapytanie o pojęcia (host/brain/learn.py): JSON, który przyjmuje parse_llm."""
    return [] if m.parse_llm(raw, cfg) is not None else ["bad_json"]


def mentions(text: str, words: Sequence[str]) -> bool:
    """Czy tekst zawiera każde ze słów, porównując rdzenie (odmiana nie przeszkadza)."""
    stems = {m.stem(w) for w in _WORD.findall(text)}
    return all(m.stem(w) in stems for w in words)


def _claims(pattern: re.Pattern[str], text: str) -> bool:
    for match in pattern.finditer(text):
        start = text.rfind(".", 0, match.start()) + 1  # zaprzeczenie liczy się w tym samym zdaniu, przed frazą
        if not _NEGATION.search(text[start:match.start()]):
            return True
    return False


def _polish(text: str) -> bool:
    words = [w.lower() for w in _WORD.findall(text)]
    if len(words) < 6:
        return True  # za krótkie na ocenę
    pl = sum(w in _PL or any(ch in "ąćęłńóśźż" for ch in w) for w in words)
    en = sum(w in _EN for w in words)
    return pl > en and pl >= len(words) * 0.15


def _valid_args(args: Any, schema: dict[str, Any]) -> bool:
    if not isinstance(args, dict):
        return False
    props = schema.get("properties", {})
    if any(k not in props for k in args) or any(k not in args for k in schema.get("required", [])):
        return False
    for key, value in args.items():
        if not isinstance(value, str) or not value.strip():
            return False
        enum = props[key].get("enum")
        if enum is not None and value not in enum:
            return False
    return True
