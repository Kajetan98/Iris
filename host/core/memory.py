"""Pamięć IRIS jako graf pojęć (docs/spec.md, sekcja 9, "Pamięć: graf pojęć").

Czyste, deterministyczne reguły bez bazy i bez modelu językowego:
- siła pojęć i połączeń, zanik, wzmacnianie i zapominanie;
- wyciąganie pojęć heurystyką i z odpowiedzi LLM, filtr liczb i danych wrażliwych;
- polecenia pamięci, reakcje i teksty odpowiedzi.

Funkcje `plan_*` zwracają, co zmienić; zapis robi host/store, a kolejność wywołań host/api.
Te same reguły ma aplikacja Android (android/core), zgodność sprawdzają wspólne wektory testowe.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from host.core.config import DEFAULT_MEMORY_DOMAIN, MEMORY_DOMAIN_IDS, MemoryConfig
from host.store.models import Concept, Link

DAY_S = 86400.0
MAX_LABEL_CHARS = 40
MIN_COMMAND_CHARS = 3
MAX_COMMAND_CHARS = 300

# Końcówki odcinane przez `stem`, w kolejności sprawdzania.
_SUFFIXES = ("owie", "ami", "ach", "ego", "emu", "ymi", "imi", "ów", "om", "iu", "ia", "ie",
             "ią", "ię", "ą", "ę", "y", "i", "a", "e", "u", "o")
_TOKEN = re.compile(r"[^\W_]+")  # litery i cyfry; słowo z cyfrą jest potem pomijane


# ---- siła -------------------------------------------------------------------------------------------


def effective(strength: float, last_used: datetime, now: datetime, half_life_days: float) -> float:
    """Siła bieżąca: s · 0,5^(wiek / okres połowicznego zaniku). Wiek ujemny (zegar cofnięty) to zero."""
    age = max(0.0, (now - last_used).total_seconds())
    return strength * 0.5 ** (age / (half_life_days * DAY_S))


def toward_one(e: float, gain: float) -> float:
    """Wzmocnienie: e + (1 − e) · gain."""
    return e + (1.0 - e) * gain


def concept_strength(c: Concept, now: datetime, cfg: MemoryConfig) -> float:
    return effective(c.strength, c.last_used, now, cfg.half_life_days)


def link_strength(link: Link, now: datetime, cfg: MemoryConfig) -> float:
    return effective(link.strength, link.last_used, now, cfg.half_life_days)


# ---- wyciąganie pojęć ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Extraction:
    concepts: tuple[tuple[str, str], ...] = ()  # (klucz, etykieta)
    domain: str = DEFAULT_MEMORY_DOMAIN
    source: Literal["heuristic", "llm"] = "heuristic"


def stem(word: str) -> str:
    """Przybliżony rdzeń polskiego słowa: łączy "lampa", "lampę" i "lampie" w "lamp"."""
    s = word.lower()
    if len(s) <= 4:
        return s
    for suffix in _SUFFIXES:
        if s.endswith(suffix) and len(s) - len(suffix) >= 4:
            return s[: -len(suffix)]
    return s


def _words(text: str) -> list[str]:
    """Słowa z liter; słowo z cyfrą jest pomijane w całości ("SpO2", "12")."""
    return [w for w in _TOKEN.findall(text or "") if not any(ch.isdigit() for ch in w)]


def concept_key(label: str) -> str:
    """Klucz pojęcia: rdzenie jego słów połączone spacją; pusty, gdy etykieta nie ma słów z liter."""
    return " ".join(stem(w) for w in _words(label))


def _is_acronym(word: str) -> bool:
    return len(word) >= 2 and word == word.upper() and word != word.lower()


def domain_of(text: str, cfg: MemoryConfig) -> str:
    """Domena z największą liczbą trafionych rdzeni (przy remisie wcześniejsza na liście), inaczej "rozmowa"."""
    words = [w.lower() for w in _words(text)]
    best, best_hits = DEFAULT_MEMORY_DOMAIN, 0
    for domain in MEMORY_DOMAIN_IDS:
        stems_ = cfg.domain_words.get(domain, [])
        hits = sum(1 for s in stems_ if any(w.startswith(s) for w in words))
        if hits > best_hits:
            best, best_hits = domain, hits
    return best


def heuristic(text: str, cfg: MemoryConfig) -> Extraction:
    """Pojęcia bez LLM: słowa z liter, bez krótkich i pomocniczych, skróty w oryginalnej pisowni."""
    stop = {w.lower() for w in cfg.stop_words}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for w in _words(text):
        acronym = _is_acronym(w)
        if not acronym and (len(w) < 3 or w.lower() in stop):
            continue
        key = stem(w)
        if key in seen:
            continue
        seen.add(key)
        out.append((key, w if acronym else w.lower()))
        if len(out) >= cfg.max_concepts:
            break
    return Extraction(tuple(out), domain_of(text, cfg), "heuristic")


def parse_llm(raw: str, cfg: MemoryConfig) -> Extraction | None:
    """Odpowiedź LLM `{"domain": ..., "concepts": [...]}` albo None, gdy jest niepoprawna."""
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("concepts", []), list):
        return None
    domain = data.get("domain")
    domain = domain if domain in MEMORY_DOMAIN_IDS else DEFAULT_MEMORY_DOMAIN
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in data.get("concepts", []):
        if not isinstance(item, str):
            continue
        label = re.sub(r"\s+", " ", item).strip()[:MAX_LABEL_CHARS].strip()
        key = concept_key(label)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((key, label))
        if len(out) >= cfg.max_concepts:
            break
    return Extraction(tuple(out), domain, "llm")


def is_sensitive(text: str, stems_: Sequence[str]) -> bool:
    """Czy tekst ma słowo zaczynające się od rdzenia danych wrażliwych."""
    words = [w.lower() for w in _TOKEN.findall(text or "")]
    return any(w.startswith(s) for w in words for s in stems_)


def keep(extraction: Extraction, cfg: MemoryConfig, *, allow_sensitive: bool) -> Extraction:
    """Filtr pamięci: bez liczb zawsze, bez danych wrażliwych poza poleceniem "zapamiętaj"."""
    kept = tuple(
        (k, label) for k, label in extraction.concepts
        if not any(ch.isdigit() for ch in label)
        and (allow_sensitive or not is_sensitive(label, cfg.sensitive_words))
    )
    return replace(extraction, concepts=kept)


# ---- zmiany w grafie ----------------------------------------------------------------------------------


@dataclass
class LearnPlan:
    concepts: list[Concept] = field(default_factory=list)  # wiersze do zapisania (nowe i zmienione)
    links: list[Link] = field(default_factory=list)
    fresh: list[str] = field(default_factory=list)  # etykiety nowych pojęć
    reinforced: list[str] = field(default_factory=list)  # etykiety wzmocnionych
    domain: str = DEFAULT_MEMORY_DOMAIN


def link_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def plan_learn(
    extraction: Extraction,
    known: Mapping[str, Concept],
    links: Mapping[tuple[str, str], Link],
    now: datetime,
    cfg: MemoryConfig,
    domain: str | None = None,
) -> LearnPlan:
    """Nauka: nowe pojęcia dostają `initial`, znane rosną o `gain_concept`, każda para dostaje połączenie.

    `known` i `links` muszą zawierać pojęcia z `extraction` i połączenia między nimi, jeśli istnieją.
    """
    domain = domain if domain in MEMORY_DOMAIN_IDS else extraction.domain
    if domain not in MEMORY_DOMAIN_IDS:
        domain = DEFAULT_MEMORY_DOMAIN
    plan = LearnPlan(domain=domain)
    keys: list[str] = []
    for key, label in extraction.concepts:
        if not key or key in keys:
            continue
        keys.append(key)
        old = known.get(key)
        if old is None:
            plan.concepts.append(Concept(key, label[:MAX_LABEL_CHARS], cfg.initial, now, 1, now, {domain: 1}))
            plan.fresh.append(label)
        else:
            domains = dict(old.domains)
            domains[domain] = domains.get(domain, 0) + 1
            e = concept_strength(old, now, cfg)
            plan.concepts.append(replace(old, strength=toward_one(e, cfg.gain_concept), last_used=now,
                                         hits=old.hits + 1, domains=domains))
            plan.reinforced.append(label)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = link_pair(keys[i], keys[j])
            old_link = links.get((a, b))
            if old_link is None:
                plan.links.append(Link(a, b, cfg.gain_link, now))
            else:
                e = link_strength(old_link, now, cfg)
                plan.links.append(Link(a, b, toward_one(e, cfg.gain_link), now))
    return plan


@dataclass(frozen=True)
class RecallHit:
    key: str
    label: str
    score: float
    via: Literal["match", "link"]


def plan_recall(
    keys: Sequence[str],
    known: Mapping[str, Concept],
    links: Iterable[Link],
    now: datetime,
    cfg: MemoryConfig,
    k: int | None = None,
) -> tuple[list[RecallHit], list[Concept]]:
    """Pojęcia o podanych kluczach i ich najmocniej połączeni sąsiedzi; pasujące są wzmacniane.

    `known` musi zawierać pasujące pojęcia i ich sąsiadów, `links` połączenia pasujących pojęć.
    Zwraca (wyniki malejąco po wyniku, przy remisie po kluczu; wzmocnione pojęcia do zapisania).
    """
    k = cfg.recall_k if k is None else k
    wanted = list(dict.fromkeys(x for x in keys if x))
    matches = [known[x] for x in wanted if x in known]
    match_keys = {c.key for c in matches}
    scores: dict[str, tuple[float, str, str]] = {
        c.key: (concept_strength(c, now, cfg), c.label, "match") for c in matches
    }
    for link in links:
        for this, other in ((link.a, link.b), (link.b, link.a)):
            if this not in match_keys or other in match_keys or other not in known:
                continue
            score = link_strength(link, now, cfg) * concept_strength(known[other], now, cfg)
            if other not in scores or scores[other][0] < score:
                scores[other] = (score, known[other].label, "link")
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1][0], kv[0]))[:k]
    hits = [RecallHit(key, label, round(score, 4), via) for key, (score, label, via) in ranked]  # type: ignore[arg-type]
    reinforced = [
        replace(c, strength=toward_one(concept_strength(c, now, cfg), cfg.gain_recall), last_used=now)
        for c in matches
    ]
    return hits, reinforced


def plan_prune(
    concepts: Iterable[Concept], links: Iterable[Link], now: datetime, cfg: MemoryConfig
) -> tuple[list[str], list[tuple[str, str]]]:
    """Klucze pojęć i pary połączeń o sile bieżącej poniżej `forget_below`."""
    dead = sorted(c.key for c in concepts if concept_strength(c, now, cfg) < cfg.forget_below)
    weak = sorted((l.a, l.b) for l in links if link_strength(l, now, cfg) < cfg.forget_below)
    return dead, weak


def plan_praise(concepts: Iterable[Concept], now: datetime, cfg: MemoryConfig) -> list[Concept]:
    """Pochwała: wzmocnienie jak przy przypomnieniu."""
    return [replace(c, strength=toward_one(concept_strength(c, now, cfg), cfg.gain_recall), last_used=now)
            for c in concepts]


def plan_criticism(
    concepts: Iterable[Concept], now: datetime, cfg: MemoryConfig
) -> tuple[list[Concept], list[str]]:
    """Krytyka: siła bieżąca razy (1 − feedback_down); poniżej progu pojęcie jest usuwane."""
    kept: list[Concept] = []
    dead: list[str] = []
    for c in concepts:
        s = concept_strength(c, now, cfg) * (1.0 - cfg.feedback_down)
        if s < cfg.forget_below:
            dead.append(c.key)
        else:
            kept.append(replace(c, strength=s, last_used=now))
    return kept, dead


def strongest(concepts: Iterable[Concept], now: datetime, cfg: MemoryConfig, n: int) -> list[Concept]:
    """`n` pojęć o największej sile bieżącej (przy remisie po kluczu)."""
    return sorted(concepts, key=lambda c: (-concept_strength(c, now, cfg), c.key))[:n]


def main_domain(c: Concept) -> str:
    """Domena, w której pojęcie padło najczęściej (przy remisie wcześniejsza na liście)."""
    if not c.domains:
        return DEFAULT_MEMORY_DOMAIN
    return max(MEMORY_DOMAIN_IDS, key=lambda d: (c.domains.get(d, 0), -MEMORY_DOMAIN_IDS.index(d)))


# ---- polecenia i reakcje ------------------------------------------------------------------------------

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
    _PREFIX + r"(?:co\s+(?:o\s+mnie\s+wiesz|wiesz\s+o\s+mnie|pamiętasz)"
    r"|czego\s+się\s+o\s+mnie\s+nauczył(?:aś|eś))$",
    re.IGNORECASE,
)
_IRIS = re.compile(r"^iris[\s,]+")
# Reakcje to cała wypowiedź; dłuższe zdanie z takim słowem na początku ("źle się czuję") to zwykła rozmowa.
_PRAISE = frozenset({"dobrze", "świetnie", "dobra odpowiedź", "trafnie", "dokładnie tak", "dzięki", "dziękuję"})
_CRITICISM = frozenset({"źle", "to nie tak", "nie tak", "nie o to pytałem", "nie o to chodziło", "to nieprawda",
                        "mylisz się"})
_YES = re.compile(r"^(?:tak|zgadza\s+się|dokładnie|owszem)\b", re.IGNORECASE)
_NO = re.compile(r"^(?:nie\s+zgadza\s+się|nieprawda|nie)\b", re.IGNORECASE)


@dataclass(frozen=True)
class MemoryCommand:
    kind: Literal["remember", "forget", "forget_all", "recall"]
    text: str = ""


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text.rstrip(".!?…").strip()


def parse_command(text: str) -> MemoryCommand | None:
    """Polecenie pamięci albo None. Tekst polecenia zachowuje wielkość liter wypowiedzi."""
    t = _clean(text).rstrip("?").strip()
    if _RECALL.match(t):
        return MemoryCommand("recall")
    if _FORGET_ALL.match(t):
        return MemoryCommand("forget_all")
    for kind, pattern in (("remember", _REMEMBER), ("forget", _FORGET)):
        m = pattern.match(t)
        if m:
            x = _clean(m.group("x")).strip("\"'„”«» ").strip()
            if len(x) >= MIN_COMMAND_CHARS:
                return MemoryCommand(kind, x[:MAX_COMMAND_CHARS])  # type: ignore[arg-type]
            return None
    return None


def parse_answer(text: str) -> bool | None:
    """Odpowiedź na pytanie z "zapomnij wszystko": True ("tak"), False ("nie"), None (inna wypowiedź)."""
    t = _clean(text)
    if _YES.match(t):
        return True
    if _NO.match(t):
        return False
    return None


def parse_feedback(text: str) -> Literal["praise", "criticism"] | None:
    """Pochwała albo krytyka ostatniej odpowiedzi, inaczej None."""
    t = _IRIS.sub("", _clean(text).lower()).strip(" ,;:")
    if t in _PRAISE:
        return "praise"
    if t in _CRITICISM:
        return "criticism"
    return None


# ---- teksty odpowiedzi --------------------------------------------------------------------------------


def plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def concepts_count(n: int) -> str:
    return f"{n} {plural(n, 'pojęcie', 'pojęcia', 'pojęć')}"


def remember_text(labels: Sequence[str]) -> str:
    if not labels:
        return "Nie znalazłem w tym zdaniu niczego do zapamiętania."
    return f"Zapamiętane: {', '.join(labels)}."


def forget_text(labels: Sequence[str]) -> str:
    if not labels:
        return "Nie pamiętam takiego pojęcia."
    return f"Zapomniane: {', '.join(labels)}."


def forget_all_question(n: int) -> str:
    if n == 0:
        return "Nic nie pamiętam, nie ma czego usuwać."
    return f"Na pewno usunąć całą pamięć ({concepts_count(n)})? Powiedz tak albo nie."


FORGET_ALL_DONE = "Pamięć wyczyszczona."
FORGET_ALL_KEPT = "Dobrze, niczego nie usuwam."
PRAISE_TEXT = "Dobrze, wzmacniam pojęcia użyte w tej odpowiedzi."
CRITICISM_TEXT = "Dobrze, osłabiam pojęcia użyte w tej odpowiedzi."


def recall_text(top_labels: Sequence[str], total: int) -> str:
    """Odpowiedź na "co o mnie wiesz": najsilniejsze pojęcia i ich łączna liczba."""
    if total == 0:
        return "Jeszcze nic nie pamiętam. Możesz powiedzieć: zapamiętaj, że ..."
    return f"Najmocniej kojarzę: {', '.join(top_labels)}. Razem pamiętam {concepts_count(total)}."


def tool_recall_text(hits: Sequence[RecallHit]) -> str:
    """Wynik narzędzia `iris_recall` dla modelu: jeden wiersz na pojęcie."""
    if not hits:
        return "Nic na ten temat nie pamiętam."
    lines = []
    for h in hits:
        lines.append(f"{h.label} (siła {decimal_pl(h.score)}, {'wprost' if h.via == 'match' else 'powiązane'})")
    return "\n".join(lines)


def decimal_pl(x: float) -> str:
    """Liczba 0-1 z dwoma miejscami po przecinku, zaokrąglenie połówek w górę (jak w Kotlinie), np. "0,63".

    Liczy na liczbach całkowitych z wartości zaokrąglonej do 4 miejsc, bo zaokrąglanie formatu dla
    liczb zmiennoprzecinkowych różni się między językami.
    """
    n = round(x * 10000)
    hundredths = (n + 50) // 100
    return f"{hundredths // 100},{hundredths % 100:02d}"


def tool_remember_text(fresh: Sequence[str], reinforced: Sequence[str]) -> str:
    parts = []
    if fresh:
        parts.append("nowe: " + ", ".join(fresh))
    if reinforced:
        parts.append("wzmocnione: " + ", ".join(reinforced))
    return "; ".join(parts) or "Nic do zapamiętania."
