"""Wspólne przypadki testowe reguł pamięci dla aplikacji Android (docs/spec.md, sekcja 14).

Wyniki reguł hosta (host/core/memory.py, host/brain/learn.py, host/brain/prompt.py, host/api/memory.py)
zapisane w android/core/src/test/resources/memory_vectors.json. Testy Kotlina odtwarzają każdy przypadek,
a host/tests/test_android_vectors.py sprawdza, że plik jest aktualny.

    python -m host.tests.make_android_vectors
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from host.api.memory import Memory
from host.brain.learn import CONCEPTS_PROMPT
from host.brain.prompt import clean_for_speech
from host.core import memory as m
from host.core.clock import ManualClock
from host.core.config import (
    DOMAIN_WORDS,
    MEMORY_DOMAINS,
    MEMORY_STOP_WORDS,
    SENSITIVE_STEMS,
    Config,
    LLMConfig,
    MemoryConfig,
)
from host.store import Store
from host.store.models import Concept, Link

VECTORS_PATH = Path(__file__).resolve().parents[2] / "android/core/src/test/resources/memory_vectors.json"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
CFG = MemoryConfig()


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def at(days: float = 0.0) -> datetime:
    return T0 + timedelta(days=days)


def concept_json(c: Concept) -> dict[str, Any]:
    return {"key": c.key, "label": c.label, "strength": c.strength, "last_used": iso(c.last_used), "hits": c.hits,
            "created_at": iso(c.created_at), "domains": dict(c.domains)}


def link_json(l: Link) -> dict[str, Any]:
    return {"a": l.a, "b": l.b, "strength": l.strength, "last_used": iso(l.last_used)}


def ext_json(e: m.Extraction | None) -> dict[str, Any] | None:
    if e is None:
        return None
    return {"concepts": [list(c) for c in e.concepts], "domain": e.domain, "source": e.source}


def c(key: str, label: str | None = None, strength: float = 0.35, days: float = 0.0, hits: int = 1,
      domains: dict[str, int] | None = None) -> Concept:
    return Concept(key, label or key, strength, at(days), hits, at(days), domains or {"rozmowa": 1})


WORDS = ["lampa", "lampę", "lampie", "Lampami", "dom", "kota", "salonie", "panowie", "spotkaniu", "Burek", "AI",
         "kawę", "kawie", "mlekiem", "ŻÓŁW", "żółwia", "imieniu", "Kraków", "dzieci", "x"]

TEXTS = [
    "Zapal lampę w salonie, IRIS. Moje tętno to 120 a SpO2 96, AI też",
    "Przypomnij mi jutro o spotkaniu",
    "Mój pies Burek lubi spacery",
    "Opowiedz o kosmosie",
    "zapal lampę jutro rano",
    "planeta i samolot",
    "lampa lampę lampie",
    "alfa beta gamma delta epsilon zeta theta jota kappa lambda",
    "",
    "   ",
    "USA i NATO, a także UE",
    "Ania_Kowalska napisała: ŻÓŁW i żółwia",
    "Mieszkam na ulicy Polnej 12 w Krakowie",
    "pies, psa, psem! kot?",
    "Lubię jazz i książki o kosmosie",
]

LLM_REPLIES = [
    'Oto wynik: {"domain": "dom", "concepts": ["lampa", "salon", "lampa", "Kraków 2"]}',
    '{"domain": "kosmos", "concepts": ["mars"]}',
    '{"concepts": ["' + "a" * 60 + '"]}',
    '```json\n{"domain": "ty", "concepts": ["pies", "Burek", "  wiele   słów  naraz "]}\n```',
    '{"concepts": [{"name": "lampa"}, 0.5, "salon", null, true]}',
    '{"domain": 5, "concepts": []}',
    '{"domain": "czas"}',
    "", "brak", "{nie json}", '["lampa"]', '{"concepts": "lampa"}', "{}", '{"concepts": ["1234", "!!!"]}',
    '{"concepts": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]}',
]

COMMANDS = [
    "Zapamiętaj, że lubię kawę z mlekiem.", "IRIS, zapisz sobie: Burek to mój pies", "pamiętaj, że jutro jest spotkanie",
    "Zapomnij o lampie", "zapomnij, że lubię kawę", "Zapomnij wszystko!", "zapomnij o mnie wszystko", "co o mnie wiesz?",
    "Co pamiętasz?", "czego się o mnie nauczyłeś", "czego się o mnie nauczyłaś", "zapamiętaj to", "co pamiętasz o lampie",
    "pamiętaj o mnie", "zapal lampę", "  Zapamiętaj,   że   \"Ania\"  …", "IRIS zapomnij o „kawie”", "ZAPAMIĘTAJ ŻE KOT",
    "zapamiętaj sobie, że " + "x" * 400,
]
ANSWERS = ["Tak, usuń", "tak", "nie zgadza się", "Nie.", "nieważne", "może", "Dokładnie", "owszem!", "nieprawda", "takie sobie"]
FEEDBACK = ["IRIS, dzięki!", "to nie tak", "źle się czuję", "Świetnie.", "dobra odpowiedź", "dobrze, a teraz?", "mylisz się",
            "iris dziękuję", "Nie o to chodziło"]
SPEECH = ["**Dobrze**, już mówię.", "# Tytuł\nTreść", "- jeden\n- dwa\n* trzy", "Linia pierwsza.\n\nLinia druga.",
          "Użyj `ls -l` teraz", "```kod\nx = 1\n```Po kodzie.", "> cytat\n_ważne_"]


def rules() -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["stem"] = [{"word": w, "out": m.stem(w)} for w in WORDS]
    out["heuristic"] = [{"text": t, "out": ext_json(m.heuristic(t, CFG))} for t in TEXTS]
    out["domain_of"] = [{"text": t, "out": m.domain_of(t, CFG)} for t in TEXTS]
    out["concept_key"] = [{"label": l, "out": m.concept_key(l)} for l in ["Kraków 2", "lampa w salonie", "", "!!!", "SpO2 i tętno"]]
    out["parse_llm"] = [{"raw": r, "out": ext_json(m.parse_llm(r, CFG))} for r in LLM_REPLIES]
    out["is_sensitive"] = [{"text": t, "out": m.is_sensitive(t, CFG.sensitive_words)} for t in TEXTS + ["biorę leki", "LEKI"]]
    keep_in = m.Extraction((("krak", "Kraków 2"), ("tętn", "tętno"), ("lamp", "lampa"), ("cukrzyc", "cukrzyca"),
                            ("spo", "SpO2")), "dom")
    out["keep"] = [{"in": ext_json(keep_in), "allow_sensitive": flag,
                    "out": ext_json(m.keep(keep_in, CFG, allow_sensitive=flag))} for flag in (False, True)]
    out["effective"] = [{"strength": s, "last_used": iso(at(0)), "now": iso(at(d)), "half_life_days": h,
                         "out": m.effective(s, at(0), at(d), h)}
                        for s, d, h in [(0.8, 14, 14), (0.8, 28, 14), (0.8, -3, 14), (0.35, 50, 14), (1.0, 0.5, 3)]]

    learn_cases = []
    known = {"lamp": c("lamp", "lampa", 0.5, -14, 2, {"dom": 2})}
    for ext, kn, links, domain in [
        (m.Extraction((("lamp", "lampę"), ("salon", "salonie"), ("lamp", "lampie")), "dom"), known, {}, None),
        (m.Extraction((("salon", "salon"), ("lamp", "lampa")), "rozmowa"), {"lamp": c("lamp"), "salon": c("salon")},
         {("lamp", "salon"): Link("lamp", "salon", 0.6, at(-14))}, "czas"),
        (m.Extraction((("xxx", "xxx"),)), {}, {}, "kosmos"),
        (m.Extraction((("ccc", "c" * 50), ("bbb", "b"), ("aaa", "a"))), {}, {}, None),
        (m.Extraction(()), {}, {}, None),
    ]:
        plan = m.plan_learn(ext, kn, links, T0, CFG, domain)
        learn_cases.append({
            "extraction": ext_json(ext), "known": [concept_json(x) for x in kn.values()],
            "links": [link_json(x) for x in links.values()], "now": iso(T0), "domain": domain,
            "out": {"concepts": [concept_json(x) for x in plan.concepts], "links": [link_json(x) for x in plan.links],
                    "fresh": plan.fresh, "reinforced": plan.reinforced, "domain": plan.domain},
        })
    out["plan_learn"] = learn_cases

    recall_known = {
        "lamp": c("lamp", "lampa", 0.8), "salon": c("salon", "salon", 0.6), "kuchni": c("kuchni", "kuchnia", 0.9),
        "jutr": c("jutr", "jutro", 0.5), "aaaa": c("aaaa", strength=0.5), "bbbb": c("bbbb", strength=0.5),
        "cccc": c("cccc", strength=0.5, days=-7), "dddd": c("dddd", strength=0.5),
    }
    recall_links = [Link("lamp", "salon", 0.5, T0), Link("kuchni", "lamp", 0.2, T0), Link("jutr", "salon", 0.9, T0),
                    Link("lamp", "zgubion", 0.9, T0), Link("aaaa", "bbbb", 1.0, T0), Link("aaaa", "cccc", 0.4, at(-3)),
                    Link("aaaa", "dddd", 0.4, T0), Link("dddd", "lamp", 0.7, at(-1))]
    recall_cases = []
    for keys, k in [(["lamp", "lamp", "nieznane"], 8), (["aaaa", "bbbb"], 3), ([], 8), (["aaaa"], 8), (["dddd", "kuchni"], 2)]:
        hits, reinforced = m.plan_recall(keys, recall_known, recall_links, T0, CFG, k)
        recall_cases.append({"keys": keys, "k": k, "now": iso(T0),
                             "out": {"hits": [{"key": h.key, "label": h.label, "score": h.score, "via": h.via} for h in hits],
                                     "reinforced": [concept_json(x) for x in reinforced]}})
    out["plan_recall"] = {"known": [concept_json(x) for x in recall_known.values()],
                          "links": [link_json(x) for x in recall_links], "cases": recall_cases}

    later = at(60)
    prune_concepts = [c("stare"), c("swieze", days=60), c("srednie", 0.9, 10)]
    prune_links = [Link("stare", "swieze", 0.3, T0), Link("swieze", "zzzz", 0.9, later), Link("aaaa", "bbbb", 0.03, at(59))]
    dead, weak = m.plan_prune(prune_concepts, prune_links, later, CFG)
    out["plan_prune"] = {"concepts": [concept_json(x) for x in prune_concepts], "links": [link_json(x) for x in prune_links],
                         "now": iso(later), "out": {"dead": dead, "weak": [list(w) for w in weak]}}
    fb = [c("lamp", strength=0.5, days=-2), c("slab", strength=0.035)]
    kept, dead2 = m.plan_criticism(fb, T0, CFG)
    out["feedback"] = {"concepts": [concept_json(x) for x in fb], "now": iso(T0),
                       "praise": [concept_json(x) for x in m.plan_praise(fb, T0, CFG)],
                       "criticism": {"kept": [concept_json(x) for x in kept], "dead": dead2}}
    strong = [c("bbbb", strength=0.8), c("aaaa", strength=0.8), c("cccc", strength=0.9, days=-14)]
    out["strongest"] = {"concepts": [concept_json(x) for x in strong], "now": iso(at(14)), "n": 2,
                        "out": [x.key for x in m.strongest(strong, at(14), CFG, 2)]}
    out["main_domain"] = [{"domains": d, "out": m.main_domain(c("x", domains=d) if d else Concept("x", "x", 0.3, T0, 1, T0, {}))}
                          for d in [{"ty": 2, "dom": 2, "czas": 1}, {}, {"rozmowa": 3, "czas": 3}, {"ty": 1}]]

    out["parse_command"] = []
    for t in COMMANDS:
        cmd = m.parse_command(t)
        out["parse_command"].append({"text": t, "out": None if cmd is None else {"kind": cmd.kind, "text": cmd.text}})
    out["parse_answer"] = [{"text": t, "out": m.parse_answer(t)} for t in ANSWERS]
    out["parse_feedback"] = [{"text": t, "out": m.parse_feedback(t)} for t in FEEDBACK]
    out["texts"] = {
        "remember": [{"labels": l, "out": m.remember_text(l)} for l in (["kawę", "mlekiem"], [])],
        "forget": [{"labels": l, "out": m.forget_text(l)} for l in (["lampa"], [])],
        "forget_all_question": [{"n": n, "out": m.forget_all_question(n)} for n in (0, 1, 2, 5, 12, 22, 101, 112)],
        "recall": [{"labels": l, "total": t, "out": m.recall_text(l, t)} for l, t in ((["kawa", "Burek"], 5), ([], 0), (["x"], 1))],
        "decimal_pl": [{"x": x, "out": m.decimal_pl(x)} for x in (0.625, 0.0849, 0.005, 1.0, 0.0, 0.995, 0.1755, 0.6249)],
        "tool_recall": m.tool_recall_text([m.RecallHit("lamp", "lampa", 0.625, "match"),
                                           m.RecallHit("salon", "salon", 0.0849, "link")]),
        "tool_recall_empty": m.tool_recall_text([]),
        "tool_remember": [{"fresh": f, "reinforced": r, "out": m.tool_remember_text(f, r)}
                          for f, r in ((["kawa"], ["mleko"]), ([], []), (["a", "b"], []))],
        "constants": {"forget_all_done": m.FORGET_ALL_DONE, "forget_all_kept": m.FORGET_ALL_KEPT,
                      "store_failed": m.STORE_FAILED_TEXT, "praise": m.PRAISE_TEXT, "criticism": m.CRITICISM_TEXT},
    }
    out["clean_for_speech"] = [{"raw": r, "out": clean_for_speech(r)} for r in SPEECH]
    return out


# Scenariusz poleceń przez obsługę pamięci hosta (host/api/memory.py), bez LLM.
SCENARIO = [
    ("say", "Zapamiętaj, że lampa stoi w salonie."),
    ("say", "co o mnie wiesz?"),
    ("advance", 7),
    ("say", "zapamiętaj, że lampa świeci w kuchni"),
    ("say", "Co pamiętasz"),
    ("recall", "lampa"),
    ("say", "dzięki"),
    ("say", "dzięki"),
    ("recall", "kuchnia"),
    ("say", "to nie tak"),
    ("say", "Zapomnij o lampie"),
    ("say", "Zapomnij o kosmosie"),
    ("say", "zapamiętaj, że mam cukrzycę i PIN 1234"),
    ("say", "zapamiętaj, że 12 34"),
    ("say", "zapomnij wszystko"),
    ("say", "nie"),
    ("say", "zapomnij wszystko"),
    ("advance", 121 / 86400),
    ("say", "tak"),
    ("say", "zapomnij wszystko"),
    ("say", "Tak, usuń."),
    ("say", "zapomnij wszystko"),
    ("say", "zapamiętaj, że Burek to pies"),
    ("advance", 60),
    ("prune", None),
    ("say", "co o mnie wiesz"),
]


def scenario() -> list[dict[str, Any]]:
    clock = ManualClock(wall=T0)
    config = Config()
    store = Store(clock=clock)

    def guard(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    memory = Memory(config, clock, store, lambda events: None, guard, lambda: None, lambda coro: coro.close())
    steps = []
    for kind, arg in SCENARIO:
        reply: Any = None
        if kind == "say":
            reply = memory.answer(arg)
            if reply is None:
                reply = memory.command(arg)
        elif kind == "advance":
            clock.advance(arg * 86400)
        elif kind == "recall":
            hits = memory.recall(arg, by="tool")
            memory.note_recalled([h.key for h in hits])
            reply = m.tool_recall_text(hits)
        elif kind == "prune":
            memory._last_prune = None
            memory.maybe_prune()
        concepts = sorted(store.concepts().values(), key=lambda x: x.key)
        steps.append({"kind": kind, "arg": arg, "reply": reply, "now": iso(clock.wall()),
                      "concepts": [concept_json(x) for x in concepts],
                      "links": [link_json(x) for x in sorted(store.all_links(), key=lambda l: (l.a, l.b))],
                      "forgotten": store.forgotten_count()})
    return steps


def migration() -> dict[str, Any]:
    """Migracja faktów z wersji 3 do grafu prawdziwą ścieżką hosta (Store na bazie w wersji 3)."""
    import sqlite3
    import tempfile

    from host.store.schema import _MIGRATIONS

    facts = [("Mam psa o imieniu Burek", iso(at(-30))), ("Pies Burek ma 3 lata", iso(at(-29))),
             ("Biorę leki na ciśnienie", iso(at(-28))), ("12 34", iso(at(-27)))]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "v3.db"
        conn = sqlite3.connect(path)
        conn.executescript(f"BEGIN;{_MIGRATIONS[0]}PRAGMA user_version = 3;COMMIT;")
        conn.executemany("INSERT INTO facts(text, source, confidence, created_at, updated_at) VALUES (?, 'stated', 1, ?, ?)",
                         [(t, w, w) for t, w in facts])
        conn.commit()
        conn.close()
        store = Store(path)
        concepts = sorted(store.concepts().values(), key=lambda x: x.key)
        links = sorted(store.all_links(), key=lambda l: (l.a, l.b))
        store.close()
    return {"facts": [{"text": t, "updated_at": w} for t, w in facts],
            "concepts": [concept_json(x) for x in concepts], "links": [link_json(x) for x in links]}


def defaults() -> dict[str, Any]:
    llm = LLMConfig()
    cfg = MemoryConfig()
    return {
        "memory": {k: getattr(cfg, k) for k in ("enabled", "infer", "half_life_days", "initial", "gain_concept", "gain_link",
                                                "gain_recall", "forget_below", "prune_every_s", "max_concepts", "recall_k",
                                                "answer_window_s", "feedback", "feedback_down")},
        "llm": {k: getattr(llm, k) for k in ("max_tokens", "timeout_s", "retries", "history_turns", "send_memory", "tools",
                                             "max_tool_rounds", "effort", "thinking_headroom_tokens", "fallbacks")},
        "sensitive_words": SENSITIVE_STEMS,
        "domain_words": DOMAIN_WORDS,
        "stop_words": MEMORY_STOP_WORDS,
        "domains": MEMORY_DOMAINS,
    }


def render() -> str:
    data = {
        "comment": "Plik generowany: python -m host.tests.make_android_vectors (nie edytuj ręcznie).",
        "concepts_prompt": CONCEPTS_PROMPT,
        "defaults": defaults(),
        "rules": rules(),
        "scenario": scenario(),
        "migration": migration(),
    }
    return json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False) + "\n"


def main() -> int:
    VECTORS_PATH.write_text(render(), encoding="utf-8")
    print(f"zapisano {VECTORS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
