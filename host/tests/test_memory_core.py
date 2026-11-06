"""Reguły grafu pojęć (docs/spec.md, sekcja 9, "Pamięć: graf pojęć")."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from host.core import memory as m
from host.core.config import MemoryConfig
from host.store.models import Concept, Link

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
CFG = MemoryConfig()


def concept(key: str, label: str | None = None, strength: float = 0.35, at: datetime = T0, hits: int = 1,
            domains: dict[str, int] | None = None) -> Concept:
    return Concept(key, label or key, strength, at, hits, at, domains or {"rozmowa": 1})


# ---- siła ---------------------------------------------------------------------------------------------


def test_strength_halves_every_half_life_and_ignores_negative_age():
    assert m.effective(0.8, T0, T0 + timedelta(days=14), 14) == pytest.approx(0.4)
    assert m.effective(0.8, T0, T0 + timedelta(days=28), 14) == pytest.approx(0.2)
    assert m.effective(0.8, T0, T0 - timedelta(days=3), 14) == 0.8  # zegar cofnięty


def test_concept_used_once_is_forgotten_after_about_fifty_days():
    c = concept("lamp")
    assert m.concept_strength(c, T0 + timedelta(days=49), CFG) > CFG.forget_below
    assert m.concept_strength(c, T0 + timedelta(days=51), CFG) < CFG.forget_below


# ---- wyciąganie -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("word", "expected"), [
    ("lampa", "lamp"), ("lampę", "lamp"), ("lampie", "lamp"), ("Lampami", "lamp"),
    ("dom", "dom"), ("kota", "kota"),  # do 4 liter bez zmian
    ("salonie", "salon"),
    ("panowie", "panow"),  # "owie" zostawiłoby 3 litery, więc pierwsza pasująca jest "ie"
])
def test_stem(word, expected):
    assert m.stem(word) == expected


def test_heuristic_skips_numbers_short_and_helper_words_and_keeps_acronyms():
    ext = m.heuristic("Zapal lampę w salonie, IRIS. Moje tętno to 120 a SpO2 96, AI też", CFG)
    labels = [label for _, label in ext.concepts]
    assert labels == ["zapal", "lampę", "salonie", "IRIS", "tętno", "AI"]
    assert ext.domain == "dom" and ext.source == "heuristic"


def test_heuristic_merges_inflected_forms_and_stops_at_the_limit():
    ext = m.heuristic("lampa lampę lampie", CFG)
    assert ext.concepts == (("lamp", "lampa"),)
    long = m.heuristic(" ".join(["alfa", "beta", "gamma", "delta", "epsilon", "zeta", "theta", "jota",
                                 "kappa", "lambda"]), CFG)
    assert len(long.concepts) == CFG.max_concepts


def test_domain_counts_stems_at_word_start_and_prefers_earlier_domain_on_tie():
    assert m.domain_of("Przypomnij mi jutro o spotkaniu", CFG) == "czas"
    assert m.domain_of("Mój pies lubi spacery", CFG) == "ty"
    assert m.domain_of("Opowiedz o kosmosie", CFG) == "rozmowa"
    assert m.domain_of("zapal lampę jutro rano", CFG) == "dom"  # remis 2:2, wygrywa wcześniejsza domena
    assert m.domain_of("planeta", CFG) == "czas"  # dopasowanie do początku słowa: "plan"
    assert m.domain_of("samolot", CFG) == "rozmowa"  # "lot" nie jest na początku


def test_parse_llm_reply():
    ext = m.parse_llm('Oto wynik: {"domain": "dom", "concepts": ["lampa", "salon", "lampa", "Kraków 2"]}', CFG)
    assert ext is not None and ext.source == "llm" and ext.domain == "dom"
    assert ext.concepts == (("lamp", "lampa"), ("salon", "salon"), ("krak", "Kraków 2"))  # liczbę usuwa filtr
    assert m.parse_llm('{"domain": "kosmos", "concepts": ["mars"]}', CFG).domain == "rozmowa"
    long = m.parse_llm('{"concepts": ["' + "a" * 60 + '"]}', CFG)
    assert len(long.concepts[0][1]) == m.MAX_LABEL_CHARS


def test_parse_llm_skips_non_text_items():
    ext = m.parse_llm('{"concepts": [{"name": "lampa"}, 0.5, "salon"]}', CFG)
    assert ext.concepts == (("salon", "salon"),)


@pytest.mark.parametrize("raw", ["", "brak", "{nie json}", '["lampa"]', '{"concepts": "lampa"}'])
def test_parse_llm_rejects_bad_replies(raw):
    assert m.parse_llm(raw, CFG) is None


def test_filter_drops_numbers_always_and_sensitive_unless_told():
    ext = m.Extraction((("kraków", "Kraków 2"), ("tętn", "tętno"), ("lamp", "lampa"), ("cukrzyc", "cukrzyca")))
    assert [l for _, l in m.keep(ext, CFG, allow_sensitive=False).concepts] == ["lampa"]
    assert [l for _, l in m.keep(ext, CFG, allow_sensitive=True).concepts] == ["tętno", "lampa", "cukrzyca"]


def test_sensitive_utterance_detection():
    assert m.is_sensitive("Mieszkam na ulicy Polnej", CFG.sensitive_words)
    assert m.is_sensitive("biorę leki", CFG.sensitive_words)
    assert not m.is_sensitive("zapal lampę", CFG.sensitive_words)


# ---- nauka ----------------------------------------------------------------------------------------------


def test_learning_new_and_known_concepts_with_links():
    known = {"lamp": concept("lamp", "lampa", strength=0.5, at=T0 - timedelta(days=14), hits=2,
                             domains={"dom": 2})}
    ext = m.Extraction((("lamp", "lampę"), ("salon", "salonie"), ("lamp", "lampie")), "dom")
    plan = m.plan_learn(ext, known, {}, T0, CFG)
    by_key = {c.key: c for c in plan.concepts}
    assert plan.fresh == ["salonie"] and plan.reinforced == ["lampę"] and plan.domain == "dom"
    lamp = by_key["lamp"]
    assert lamp.label == "lampa"  # etykieta z pierwszego zapisu zostaje
    assert lamp.strength == pytest.approx(0.25 + 0.75 * 0.4)  # e = 0,25 po 14 dniach
    assert (lamp.hits, lamp.domains, lamp.last_used) == (3, {"dom": 3}, T0)
    assert by_key["salon"].strength == CFG.initial and by_key["salon"].domains == {"dom": 1}
    assert plan.links == [Link("lamp", "salon", CFG.gain_link, T0)]


def test_learning_strengthens_an_existing_link_and_accepts_a_domain_override():
    links = {("lamp", "salon"): Link("lamp", "salon", 0.6, T0 - timedelta(days=14))}
    known = {"lamp": concept("lamp"), "salon": concept("salon")}
    plan = m.plan_learn(m.Extraction((("salon", "salon"), ("lamp", "lampa")), "rozmowa"), known, links, T0, CFG,
                        domain="czas")
    assert plan.links[0].strength == pytest.approx(0.3 + 0.7 * 0.3)
    assert plan.domain == "czas" and all(c.domains.get("czas") == 1 for c in plan.concepts)
    assert m.plan_learn(m.Extraction((("x" * 3, "xxx"),)), {}, {}, T0, CFG, domain="kosmos").domain == "rozmowa"


# ---- przypomnienie, zapominanie, reakcje ------------------------------------------------------------------


def test_recall_ranks_matches_and_strongest_neighbour_link_and_reinforces_matches():
    known = {
        "lamp": concept("lamp", "lampa", 0.8),
        "salon": concept("salon", "salon", 0.6),
        "kuchni": concept("kuchni", "kuchnia", 0.9),
        "jutr": concept("jutr", "jutro", 0.5),
    }
    links = [
        Link("lamp", "salon", 0.5, T0),
        Link("kuchni", "lamp", 0.2, T0),
        Link("jutr", "salon", 0.9, T0),  # nie dotyczy pasującego pojęcia, pomijane
        Link("lamp", "zgubion", 0.9, T0),  # sąsiad spoza known, pomijany
    ]
    hits, reinforced = m.plan_recall(["lamp", "lamp", "nieznane"], known, links, T0, CFG)
    assert [(h.key, h.via, h.score) for h in hits] == [("lamp", "match", 0.8), ("salon", "link", 0.3),
                                                       ("kuchni", "link", 0.18)]
    assert reinforced == [Concept("lamp", "lampa", pytest.approx(0.8 + 0.2 * 0.15), T0, 1, T0, {"rozmowa": 1})]


def test_recall_does_not_turn_a_match_into_a_neighbour_and_respects_k_and_ties():
    known = {k: concept(k, strength=0.5) for k in ("aaaa", "bbbb", "cccc", "dddd")}
    links = [Link("aaaa", "bbbb", 1.0, T0), Link("aaaa", "cccc", 0.4, T0), Link("aaaa", "dddd", 0.4, T0)]
    hits, _ = m.plan_recall(["aaaa", "bbbb"], known, links, T0, CFG, k=3)
    assert [(h.key, h.via) for h in hits] == [("aaaa", "match"), ("bbbb", "match"), ("cccc", "link")]
    assert m.plan_recall([], known, links, T0, CFG) == ([], [])


def test_prune_finds_concepts_and_links_below_the_threshold():
    later = T0 + timedelta(days=60)
    concepts = [concept("stare"), concept("swieze", at=later)]
    links = [Link("stare", "swieze", 0.3, T0), Link("swieze", "zzzz", 0.9, later)]
    assert m.plan_prune(concepts, links, later, CFG) == (["stare"], [("stare", "swieze")])


def test_praise_and_criticism():
    c = concept("lamp", strength=0.5)
    assert m.plan_praise([c], T0, CFG)[0].strength == pytest.approx(0.5 + 0.5 * 0.15)
    kept, dead = m.plan_criticism([c, concept("slab", strength=0.035)], T0, CFG)
    assert [k.strength for k in kept] == [pytest.approx(0.4)] and dead == ["slab"]


def test_strongest_and_main_domain():
    later = T0 + timedelta(days=14)
    cs = [concept("bbbb", strength=0.8), concept("aaaa", strength=0.8), concept("cccc", strength=0.9, at=T0 - timedelta(days=14))]
    assert [c.key for c in m.strongest(cs, later, CFG, 2)] == ["aaaa", "bbbb"]
    assert m.main_domain(concept("x", domains={"ty": 2, "dom": 2, "czas": 1})) == "dom"
    assert m.main_domain(concept("x", domains={})) == "rozmowa"


# ---- polecenia i teksty -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "kind", "arg"), [
    ("Zapamiętaj, że lubię kawę z mlekiem.", "remember", "lubię kawę z mlekiem"),
    ("IRIS, zapisz sobie: Burek to mój pies", "remember", "Burek to mój pies"),
    ("pamiętaj, że jutro jest spotkanie", "remember", "jutro jest spotkanie"),
    ("Zapomnij o lampie", "forget", "lampie"),
    ("zapomnij, że lubię kawę", "forget", "lubię kawę"),
    ("Zapomnij wszystko!", "forget_all", ""),
    ("co o mnie wiesz?", "recall", ""),
    ("Co pamiętasz?", "recall", ""),
    ("czego się o mnie nauczyłeś", "recall", ""),
])
def test_parse_command(text, kind, arg):
    assert m.parse_command(text) == m.MemoryCommand(kind, arg)


@pytest.mark.parametrize("text", ["zapamiętaj to", "co pamiętasz o lampie", "pamiętaj o mnie", "zapal lampę"])
def test_not_a_command(text):
    assert m.parse_command(text) is None


def test_answers_and_feedback():
    assert m.parse_answer("Tak, usuń") is True and m.parse_answer("nie zgadza się") is False
    assert m.parse_answer("nieważne") is None and m.parse_answer("może") is None
    assert m.parse_feedback("IRIS, dzięki!") == "praise" and m.parse_feedback("to nie tak") == "criticism"
    assert m.parse_feedback("źle się czuję") is None


def test_texts():
    assert m.remember_text(["kawę", "mlekiem"]) == "Zapamiętane: kawę, mlekiem."
    assert m.remember_text([]) == "Nie znalazłem w tym zdaniu niczego do zapamiętania."
    assert m.forget_text([]) == "Nie pamiętam takiego pojęcia."
    assert m.forget_all_question(0) == "Nic nie pamiętam, nie ma czego usuwać."
    assert m.forget_all_question(1) == "Na pewno usunąć całą pamięć (1 pojęcie)? Powiedz tak albo nie."
    assert m.forget_all_question(22) == "Na pewno usunąć całą pamięć (22 pojęcia)? Powiedz tak albo nie."
    assert m.forget_all_question(12) == "Na pewno usunąć całą pamięć (12 pojęć)? Powiedz tak albo nie."
    assert m.recall_text(["kawa", "Burek"], 5) == "Najmocniej kojarzę: kawa, Burek. Razem pamiętam 5 pojęć."
    assert m.recall_text([], 0).startswith("Jeszcze nic nie pamiętam.")
    hits = [m.RecallHit("lamp", "lampa", 0.625, "match"), m.RecallHit("salon", "salon", 0.0849, "link")]
    assert m.tool_recall_text(hits) == "lampa (siła 0,63, wprost)\nsalon (siła 0,08, powiązane)"
    assert m.tool_recall_text([]) == "Nic na ten temat nie pamiętam."
    assert m.tool_remember_text(["kawa"], ["mleko"]) == "nowe: kawa; wzmocnione: mleko"
    assert m.tool_remember_text([], []) == "Nic do zapamiętania."


def test_config_rejects_inconsistent_values():
    with pytest.raises(ValueError):
        MemoryConfig(forget_below=0.5, initial=0.35)
    with pytest.raises(ValueError):
        MemoryConfig(domain_words={"cialo": ["tętn"]})
