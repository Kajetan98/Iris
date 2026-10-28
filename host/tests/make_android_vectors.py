"""Wektory testowe zgodności Python/Kotlin (docs/spec.md, sekcja 14).

Wyniki reguł hosta (host/core/learning.py, host/brain/learn.py, host/brain/prompt.py, host/api/learner.py)
dla ustalonych danych wejściowych, zapisane do pliku, który odtwarzają testy Kotlina w android/core.
Uruchomienie: python -m host.tests.make_android_vectors (bez argumentów zapisuje plik w android/).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from host.api.learner import Learner
from host.brain.learn import EXTRACT_PROMPT, Guess, LearnError, parse_guesses
from host.brain.prompt import clean_for_speech, format_time, render_context, select_facts
from host.core.clock import ManualClock
from host.core.config import Config, LLMConfig
from host.core.learning import (
    best_match,
    fact_text,
    is_sensitive,
    parse_answer,
    parse_memory,
    pick_question,
    question_for,
    recall_text,
    similarity,
    stems,
)
from host.store import Store
from host.store.models import Fact

VECTORS_PATH = Path(__file__).resolve().parents[2] / "android/core/src/test/resources/learning_vectors.json"
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def fact_json(f: Fact) -> dict[str, Any]:
    return {
        "id": f.id, "text": f.text, "source": f.source, "confidence": f.confidence,
        "created_at": iso(f.created_at), "updated_at": iso(f.updated_at),
        "reinforced": f.reinforced, "asked_at": iso(f.asked_at),
    }


def mk(id_, text, source="stated", confidence=1.0, *, age_days=0.0, asked_days=None, reinforced=0) -> Fact:
    at = T0 - timedelta(days=age_days)
    asked = None if asked_days is None else T0 - timedelta(days=asked_days)
    return Fact(id_, text, source, confidence, at, at, 1, reinforced, asked)


# --- dane wejściowe ---

MEMORY_TEXTS = [
    "Zapamiętaj, że mam psa Burka.", "zapamiętaj mam psa Burka", "IRIS, zapisz sobie: pracuję w szpitalu",
    "Pamiętaj, że w piątki gram w kosza", 'Zapamiętaj o tym, że "lubię kawę"', "Zapomnij, że mam psa",
    "zapomnij o kawie", "Zapomnij wszystko.", "zapomnij o mnie wszystko", "zapomnij wszystko o mnie",
    "Co o mnie wiesz?", "IRIS, co wiesz o mnie", "Czego się o mnie nauczyłaś?", "czego się o mnie nauczyłeś",
    "Pamiętaj o zakupach", "Co wiesz o pogodzie?", "zapamiętaj to", "Czy zapamiętasz, że mam psa?",
    "Jak się masz", "przełącz na laptop", "  ZAPAMIĘTAJ,   że  lubię   jazz!!! ", "iris zapamiętaj: ŁÓDŹ to moje miasto",
    "Zapamiętaj, że „mam kota”", "zapomnij, że ", "zapomnij", "pamiętaj że mam rower…",
    "zapisz: " + "a" * 400, "Zapamiętaj,\nże mam\npsa", "iris, zapomnij o tym, że mam psa", "zapomnij że lubię kawę?",
]
ANSWER_TEXTS = [
    "Tak", "tak, zgadza się", "Zgadza się.", "Dokładnie!", "owszem", "Nie", "Nieprawda.", "nie zgadza się",
    "Nie, mam kota.", "nie - pracuję zdalnie", "nie: ab", "nie, ok", "Niedziela jest wolna", "Taksówka przyjedzie",
    "Jaka jest pogoda?", "", "  tak  ", "NIE, pracuję w Łodzi.", "nie\nmam kota", "tak.",
]
FACT_TEXTS = ["  mam   psa. ", '"lubię kawę"', "„lubię herbatę”.", "Lubię jazz!?", "…", "„a”", "To jest zdanie…"]
SIMILARITY_PAIRS = [
    ("Pracujesz w Krakowie.", "pracuję w krakowie"), ("Lubisz kawę z mlekiem.", "Lubisz kawa z mlekiem"),
    ("Masz psa o imieniu Burek.", "Masz kota."), ("o 18:00 trening", "trening o 18:00"), ("", "coś"),
    ("ŁÓDŹ to moje miasto", "łódź to miasto"), ("mam psa", "masz psa"), ("gram w kosza w piątki", "w piątki gram w kosza"),
    ("lubię_jazz 2024", "lubię jazz 2024"), ("abc abcd abcde abcdef", "abcd abcde abcdef abcdefg"),
    ("Pracuję zdalnie", "Pracujesz zdalnie"), ("kot", "kota"), ("są 3 koty", "mam 3 koty"), ("to jest", "mam masz jest"),
    ("Mieszkam w Gdyni 😀", "mieszkam w gdyni"),
]
SENSITIVE_TEXTS = [
    "Bierzesz leki na ciśnienie.", "Mieszkasz przy ulicy Długiej.", "Zarabiasz dobrze.", "Chodzisz do lekarza we wtorki.",
    "Jesteś katolikiem.", "Lubisz długo spać.", "Masz lekki rower.", "Pracujesz zdalnie.", "Grasz w szachy.",
    "Masz konto w banku", "Twój PESEL", "Głosujesz na partię", "Masz depresję", "Lubisz GPS", "mieszkasz w Gdyni",
]
CLEAN_TEXTS = [
    "Dobrze.", "**Hej** _tam_", "# Nagłówek\nTreść", "- jeden\n- dwa", "Kod: ```print(1)``` koniec", "Linia 1\n\nLinia 2",
    "  wiele   spacji ", "> cytat", "1. punkt", "* gwiazdka", "`kod` w tekście", "", "   ", "Tekst z # w środku", "+ plus\n+ dwa",
    "```\nblok\n```", "a_b_c", "Zażółć gęślą jaźń",
]
GUESS_RAWS = [
    '[{"fakt": "Masz psa o imieniu Burek.", "pewnosc": 0.9}]',
    '```json\n[{"fakt": "Lubisz kawę.", "pewnosc": 0.7}]\n```',
    'Oto lista: [{"fakt": "Masz kota.", "pewnosc": 1.5}, {"fakt": "Lubisz jazz.", "pewnosc": -2}] koniec',
    '[{"text": "Masz rower.", "confidence": 0.5}, {"fakt": "Masz hulajnogę.", "pewność": 0.6}]',
    "[]", "brak faktów", "[{", '{"fakt": "x"}', '[{"fakt": "ab", "pewnosc": 0.5}]', '[{"fakt": "Masz psa.", "pewnosc": true}]',
    '[{"fakt": "Masz psa.", "pewnosc": "0.5"}]', '[1, "a", null, {"fakt": "Masz rybki.", "pewnosc": 1}]',
    '[{"fakt": "Pierwszy fakt.", "pewnosc": 0.5}, {"fakt": "Drugi fakt.", "pewnosc": 0.5}, '
    '{"fakt": "Trzeci fakt.", "pewnosc": 0.5}, {"fakt": "Czwarty fakt.", "pewnosc": 0.5}]',
    '[{"fakt": "  Masz   psa.  ", "pewnosc": 0.5}]', '[{"fakt": "\\"Masz psa\\"", "pewnosc": 0.5}]',
    '[[{"fakt": "Zagnieżdżony.", "pewnosc": 0.5}]]', '[{"fakt": "Masz psa.", "pewnosc": 0.5}] i jeszcze [coś]',
    '[{"fakt": "' + "a" * 301 + '", "pewnosc": 0.5}]',
]


def rules() -> dict[str, Any]:
    cfg = Config()
    stems_ = cfg.learning.sensitive_words
    out: dict[str, Any] = {}
    out["parse_memory"] = [
        {"in": t, "out": None if (c := parse_memory(t)) is None else {"kind": c.kind, "text": c.text}}
        for t in MEMORY_TEXTS
    ]
    out["parse_answer"] = [
        {"in": t, "yes": (a := parse_answer(t))[0], "correction": a[1]} for t in ANSWER_TEXTS
    ]
    out["fact_text"] = [{"in": t, "out": fact_text(t)} for t in FACT_TEXTS]
    out["stems"] = [{"in": t, "out": sorted(stems(t))} for pair in SIMILARITY_PAIRS for t in pair]
    out["similarity"] = [{"a": a, "b": b, "out": similarity(a, b)} for a, b in SIMILARITY_PAIRS]
    out["is_sensitive"] = [{"in": t, "out": is_sensitive(t, stems_)} for t in SENSITIVE_TEXTS]
    out["clean_for_speech"] = [{"in": t, "out": clean_for_speech(t)} for t in CLEAN_TEXTS]
    out["parse_guesses"] = []
    for raw in GUESS_RAWS:
        for limit in (3, 1):
            try:
                result: Any = [{"text": g.text, "confidence": g.confidence} for g in parse_guesses(raw, limit=limit)]
            except LearnError:
                result = {"error": True}
            out["parse_guesses"].append({"raw": raw, "limit": limit, "out": result})
    return out


def facts_cases() -> dict[str, Any]:
    cfg = Config()
    out: dict[str, Any] = {}
    pool = [
        mk(1, "Masz psa o imieniu Burek", age_days=0), mk(2, "Masz kota Filemona"), mk(3, "Masz psa Azora", age_days=3),
    ]
    out["best_match"] = [
        {"in": t, "min": m, "facts": [fact_json(f) for f in pool],
         "out": None if (b := best_match(t, pool, m)) is None else b.id}
        for t, m in (("mam psa", 0.5), ("kot Filemon", 0.5), ("lubię rower", 0.5), ("masz psa", 0.6), ("psa", 0.3))
    ]
    out["question_for"] = [
        {"fact": fact_json(f), "out": question_for(f)}
        for f in (mk(1, "Masz psa o imieniu Burek.", "inferred", 0.6), mk(2, "USA to twój ulubiony kraj", "inferred", 0.6),
                  mk(3, "Lubisz kawę…", "inferred", 0.6), mk(4, "  Ala   ma kota ", "inferred", 0.5))
    ]
    questions = [
        ([mk(1, "Lubisz kawę", "inferred", 0.45), mk(2, "Masz psa", "inferred", 0.65),
          mk(3, "Grasz w szachy", "inferred", 0.85, asked_days=2), mk(4, "Pracujesz zdalnie"),
          mk(5, "Jeździsz rowerem", "inferred", 0.3)]),
        ([mk(1, "Lubisz kawę", "inferred", 0.45), mk(2, "Masz psa", "inferred", 0.65),
          mk(3, "Grasz w szachy", "inferred", 0.85, asked_days=8)]),
        ([mk(1, "Pracujesz zdalnie"), mk(2, "Jeździsz rowerem", "inferred", 0.3)]),
        ([mk(1, "A fakt", "inferred", 0.6, age_days=2), mk(2, "B fakt", "inferred", 0.6, age_days=1),
          mk(3, "C fakt", "inferred", 0.6, age_days=1)]),
        ([]),
    ]
    out["pick_question"] = [
        {"facts": [fact_json(f) for f in fs], "now": iso(T0),
         "out": None if (p := pick_question(fs, T0, cfg.learning)) is None else p.id}
        for fs in questions
    ]
    recalls = [
        [],
        [mk(i, f"fakt numer {i}") for i in range(1, 8)] + [mk(10, "Lubisz kawę.", "inferred", 0.65), mk(11, "Masz psa.", "inferred", 0.3)],
        [mk(1, "Masz psa o imieniu Burek"), mk(2, "USA to twój ulubiony kraj")],
        [mk(11, "Masz psa.", "inferred", 0.3)],
        [mk(i, f"Przypuszczenie {i}", "inferred", 0.5 + i / 20) for i in range(1, 6)],
        [mk(1, "Ala", age_days=2), mk(2, "Basia", age_days=1), mk(3, "Celina", age_days=3)],
        [mk(i, f"fakt {i}") for i in range(1, 13)],
    ]
    out["recall_text"] = [
        {"facts": [fact_json(f) for f in fs], "min": 0.5, "out": recall_text(fs, 0.5)} for fs in recalls
    ]
    llm = LLMConfig()
    pool2 = [
        mk(1, "Mówi po polsku", "stated", 1.0, age_days=5), mk(2, "Lubi kawę", "inferred", 0.8, age_days=1),
        mk(3, "Ma psa", "inferred", 0.45), mk(4, "Gra w szachy", "stated", 1.0, age_days=1),
        mk(5, "Jeździ rowerem", "inferred", 0.5, age_days=2), mk(6, "Lubi jazz", "inferred", 0.8, age_days=3),
    ]
    out["select_facts"] = []
    for send in ("always", "local_only", "never"):
        for remote in (True, False):
            for max_facts in (30, 2):
                cfg_llm = LLMConfig(send_facts=send, max_facts=max_facts)
                out["select_facts"].append({
                    "facts": [fact_json(f) for f in pool2], "send_facts": send, "remote": remote, "max_facts": max_facts,
                    "min_inferred_confidence": llm.min_inferred_confidence,
                    "out": [f.id for f in select_facts(pool2, cfg_llm, remote=remote)],
                })
    now = datetime(2026, 10, 28, 14, 5)  # środa, czas lokalny bez strefy
    chosen = select_facts(pool2, LLMConfig(send_facts="always"), remote=True)
    ctx = render_context({}, chosen, now).split("\n")
    out["context"] = {
        "now": now.isoformat(),
        "facts": [fact_json(f) for f in pool2],
        "time_line": next(line for line in ctx if line.startswith("Czas: ")),
        "fact_lines": ctx[ctx.index("Znane fakty o użytkowniku:") + 1:],
        "times": [{"now": d.isoformat(), "out": format_time(d)} for d in (
            datetime(2026, 1, 5, 0, 0), datetime(2026, 2, 28, 23, 59), datetime(2026, 12, 31, 9, 7),
            datetime(2027, 3, 14, 12, 30), datetime(2026, 8, 9, 18, 45))],
    }
    return out


# --- scenariusze Learnera ---

SCENARIOS: list[dict[str, Any]] = [
    {"name": "polecenia pamięci bez modelu", "steps": [
        {"op": "say", "text": "Zapamiętaj, że mam psa Burka."},
        {"op": "say", "text": "IRIS, zapisz: pracuję w Gdyni"},
        {"op": "say", "text": "Co o mnie wiesz?"},
        {"op": "say", "text": "Zapomnij, że mam psa"},
        {"op": "say", "text": "zapomnij o rowerze"},
        {"op": "say", "text": "Jaka jest pogoda?"},
    ]},
    {"name": "podobny fakt zastępuje poprzedni", "steps": [
        {"op": "say", "text": "Zapamiętaj, że pracuję w Gdyni"},
        {"op": "say", "text": "Zapamiętaj, że pracuję w Gdańsku od marca"},
        {"op": "say", "text": "zapamiętaj: pracuję w Gdańsku od kwietnia"},
        {"op": "say", "text": "zapamiętaj: od marca pracuję w Gdańsku zdalnie"},
    ]},
    {"name": "zapomnij wszystko wymaga tak", "steps": [
        {"op": "say", "text": "zapomnij wszystko"},
        {"op": "say", "text": "zapamiętaj, że mam psa Burka"},
        {"op": "say", "text": "zapamiętaj, że gram w szachy"},
        {"op": "say", "text": "zapamiętaj, że lubię jazz"},
        {"op": "say", "text": "Zapomnij wszystko"},
        {"op": "say", "text": "nie"},
        {"op": "say", "text": "Zapomnij wszystko"},
        {"op": "say", "text": "Tak."},
        {"op": "say", "text": "zapomnij wszystko"},
    ]},
    {"name": "przypuszczenie rośnie, pytanie, potwierdzenie", "steps": [
        {"op": "merge", "guesses": [["Masz psa o imieniu Burek", 0.9]]},
        {"op": "merge", "guesses": [["Masz psa o imieniu Burek.", 0.9]]},
        {"op": "question"},
        {"op": "merge", "guesses": [["Masz psa o imieniu Burek", 0.9], ["Lubisz kawę", 0.3]]},
        {"op": "advance", "seconds": 10},
        {"op": "say", "text": "Tak, zgadza się"},
        {"op": "say", "text": "Co o mnie wiesz?"},
    ]},
    {"name": "wzmacnianie do limitu", "steps": [
        {"op": "merge", "guesses": [["Grasz w szachy", 0.8]]},
        {"op": "merge", "guesses": [["Grasz w szachy", 0.8]]},
        {"op": "merge", "guesses": [["Grasz w szachy", 0.8]]},
        {"op": "merge", "guesses": [["Grasz w szachy", 0.8]]},
        {"op": "merge", "guesses": [["Grasz w szachy", 0.8]]},
    ]},
    {"name": "zaprzeczenie z poprawką", "steps": [
        {"op": "seed", "text": "Masz psa o imieniu Burek", "source": "inferred", "confidence": 0.6},
        {"op": "question"},
        {"op": "say", "text": "Nie, mam kota Filemona."},
        {"op": "say", "text": "Co o mnie wiesz?"},
    ]},
    {"name": "samo nie usuwa przypuszczenie", "steps": [
        {"op": "seed", "text": "Lubisz kawę", "source": "inferred", "confidence": 0.7},
        {"op": "question"},
        {"op": "say", "text": "nie"},
        {"op": "say", "text": "Co o mnie wiesz?"},
    ]},
    {"name": "pytanie wygasa po oknie odpowiedzi", "steps": [
        {"op": "seed", "text": "Lubisz kawę", "source": "inferred", "confidence": 0.7},
        {"op": "question"},
        {"op": "advance", "seconds": 121},
        {"op": "say", "text": "tak"},
    ]},
    {"name": "inna wypowiedź zamyka pytanie", "steps": [
        {"op": "seed", "text": "Lubisz kawę", "source": "inferred", "confidence": 0.7},
        {"op": "question"},
        {"op": "say", "text": "Jaka jest pogoda?"},
        {"op": "say", "text": "tak"},
    ]},
    {"name": "odstęp między pytaniami i ponawianie", "steps": [
        {"op": "seed", "text": "Lubisz kawę", "source": "inferred", "confidence": 0.7},
        {"op": "seed", "text": "Masz psa", "source": "inferred", "confidence": 0.6},
        {"op": "question"},
        {"op": "say", "text": "Jaka jest pogoda?"},
        {"op": "question"},
        {"op": "advance", "seconds": 901},
        {"op": "question"},
        {"op": "say", "text": "x"},
        {"op": "advance", "seconds": 901},
        {"op": "question"},
        {"op": "advance", "seconds": 7 * 86400},
        {"op": "question"},
    ]},
    {"name": "wnioski wrażliwe, słabe i podobne do stated", "steps": [
        {"op": "say", "text": "zapamiętaj, że mam psa Burka"},
        {"op": "merge", "guesses": [
            ["Bierzesz leki na ciśnienie", 0.9], ["Lubisz kawę", 0.1], ["Masz psa Burka", 0.9], ["Lubisz jazz", 0.5],
        ]},
    ]},
    {"name": "zanikanie", "steps": [
        {"op": "seed", "text": "Lubisz kawę", "source": "inferred", "confidence": 0.45},
        {"op": "seed", "text": "Masz psa", "source": "inferred", "confidence": 0.25},
        {"op": "seed", "text": "Mieszkasz w Gdyni", "source": "stated", "confidence": 1.0},
        {"op": "decay"},
        {"op": "advance", "seconds": 31 * 86400},
        {"op": "decay"},
        {"op": "decay"},
        {"op": "advance", "seconds": 86401},
        {"op": "decay"},
        {"op": "advance", "seconds": 31 * 86400},
        {"op": "decay"},
    ]},
    {"name": "nauka wyłączona", "config": {"learning": {"enabled": False}}, "steps": [
        {"op": "say", "text": "Zapamiętaj, że mam psa Burka."},
        {"op": "say", "text": "Co o mnie wiesz?"},
        {"op": "seed", "text": "Lubisz kawę", "source": "inferred", "confidence": 0.7},
        {"op": "question"},
        {"op": "merge", "guesses": [["Masz psa", 0.9]]},
        {"op": "advance", "seconds": 40 * 86400},
        {"op": "decay"},
    ]},
    {"name": "pytania wyłączone", "config": {"learning": {"ask": False}}, "steps": [
        {"op": "seed", "text": "Lubisz kawę", "source": "inferred", "confidence": 0.7},
        {"op": "question"},
    ]},
    {"name": "tryb ciszy", "config": {"initiative": {"quiet": True}}, "steps": [
        {"op": "seed", "text": "Lubisz kawę", "source": "inferred", "confidence": 0.7},
        {"op": "question"},
    ]},
    {"name": "co o mnie wiesz z przypuszczeniami", "steps": [
        {"op": "say", "text": "zapamiętaj, że mam psa Burka"},
        {"op": "seed", "text": "Lubisz kawę", "source": "inferred", "confidence": 0.65},
        {"op": "seed", "text": "Masz rower", "source": "inferred", "confidence": 0.3},
        {"op": "seed", "text": "Grasz w szachy", "source": "inferred", "confidence": 0.9},
        {"op": "say", "text": "co o mnie wiesz"},
    ]},
    {"name": "zapomnij podobne, próg dopasowania", "steps": [
        {"op": "say", "text": "zapamiętaj, że pracuję w Gdyni jako programista"},
        {"op": "say", "text": "zapomnij o programiście"},
        {"op": "say", "text": "zapomnij, że pracuję w Gdyni"},
        {"op": "say", "text": "zapomnij, że pracuję w Gdyni"},
    ]},
    {"name": "tekst z liczbami i wielkimi literami", "steps": [
        {"op": "say", "text": "Zapamiętaj, że trening mam o 18:00"},
        {"op": "say", "text": "zapamiętaj: o 18:00 mam trening"},
        {"op": "say", "text": "zapamiętaj, że ŁÓDŹ to moje miasto"},
        {"op": "say", "text": "zapomnij o łódź"},
        {"op": "say", "text": "co o mnie wiesz"},
    ]},
]


def run_scenario(scn: dict[str, Any]) -> dict[str, Any]:
    clock = ManualClock(T0, 0.0)
    store = Store(clock=clock)
    store.begin_session()
    config = Config.model_validate(scn.get("config", {}))
    learner = Learner(config, clock, store, lambda events: None, lambda fn, *a, **k: fn(*a, **k), lambda: None)
    steps = []
    for step in scn["steps"]:
        reply: str | None = None
        op = step["op"]
        if op == "say":
            reply = learner.answer(step["text"])
            if reply is None:
                reply = learner.command(step["text"])
        elif op == "question":
            reply = learner.question()
        elif op == "merge":
            learner.merge([Guess(t, c) for t, c in step["guesses"]])
        elif op == "seed":
            store.add_fact(step["text"], step["source"], step["confidence"])
        elif op == "decay":
            learner.maybe_decay()
        elif op == "advance":
            clock.advance(step["seconds"])
        else:
            raise ValueError(op)
        steps.append({
            "reply": reply,
            "facts": [
                [f.id, f.text, f.source, round(f.confidence, 6), f.reinforced, f.asked_at is not None]
                for f in store.facts()
            ],
        })
    return {"name": scn["name"], "config": scn.get("config", {}), "steps": scn["steps"], "expected": steps}


def build() -> dict[str, Any]:
    cfg = Config()
    llm = cfg.brain.llm
    return {
        "version": 1,
        "start": iso(T0),
        "defaults": {
            "learning": cfg.learning.model_dump(),
            "llm": {
                k: getattr(llm, k)
                for k in ("max_tokens", "timeout_s", "retries", "history_turns", "max_facts",
                          "min_inferred_confidence", "effort", "thinking_headroom_tokens", "fallbacks")
            },
            "quiet": cfg.initiative.quiet,
        },
        "extract_prompt": EXTRACT_PROMPT,
        "rules": rules(),
        "facts": facts_cases(),
        "scenarios": [run_scenario(s) for s in SCENARIOS],
    }


def render() -> str:
    return json.dumps(build(), ensure_ascii=False, indent=1) + "\n"


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else VECTORS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(), encoding="utf-8")
    print(f"zapisano {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
