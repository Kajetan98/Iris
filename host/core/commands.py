"""Komendy rozpoznawane na hoście przed LLM (docs/spec.md, sekcja 1, "Komenda przełączenia").

Deterministyczne: wyrażenie regularne i porównanie nazw, bez modelu językowego.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

_SWITCH = re.compile(
    r"^(?:iris[\s,]+)?(?:przełącz(?:\s+się)?|przejdź|zmień\s+urządzenie)\s+na\s+(?P<name>.+)$"
)
# Końcówki przypadków dopisywane do id ("laptopa", "telefonu", "hermesem").
_ENDINGS = ("a", "u", "em", "ie", "owi", "y")


def _normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip().lower())
    return text.rstrip(".!?…").strip()


def parse_switch(text: str) -> str | None:
    """Nazwa urządzenia z komendy "przełącz na X" albo None, gdy to nie ta komenda."""
    m = _SWITCH.match(_normalize(text))
    if m is None:
        return None
    name = m.group("name").strip(" ,")
    return name or None


def _names(device: str, aliases: Iterable[str]) -> set[str]:
    base = device.replace("-", " ")
    names = {device, base, *(_normalize(a) for a in aliases)}
    names |= {base + ending for ending in _ENDINGS}
    return names


def resolve_device(name: str, devices: Iterable[str], aliases: Mapping[str, list[str]]) -> list[str]:
    """Urządzenia pasujące do wypowiedzianej nazwy (więcej niż jedno: niejednoznaczna)."""
    spoken = _normalize(name)
    return sorted(d for d in set(devices) | set(aliases) if spoken in _names(d, aliases.get(d, [])))
