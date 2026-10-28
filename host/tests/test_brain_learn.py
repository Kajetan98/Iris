"""Wnioski z rozmowy przez LLM (docs/spec.md, sekcja 9, "Uczenie się") i opis pamięci w prompcie."""

import asyncio

import pytest

from host.brain.learn import EXTRACT_PROMPT, Guess, LearnError, infer_facts, parse_guesses
from host.brain.llm import LLMReply, LLMUnavailable, Usage
from host.brain.prompt import CONTEXT_HEADER, SYSTEM_PROMPT
from host.tests.brain_helpers import ScriptedEngine


def run(coro):
    return asyncio.run(coro)


def test_parse_plain_and_fenced_lists():
    raw = '[{"fakt": "Masz psa o imieniu Burek.", "pewnosc": 0.8}, {"fakt": "Lubisz kawę", "pewnosc": 1}]'
    assert parse_guesses(raw, limit=3) == [Guess("Masz psa o imieniu Burek", 0.8), Guess("Lubisz kawę", 1.0)]
    fenced = 'Oto fakty:\n```json\n[{"fakt": "Grasz w szachy.", "pewnosc": 0.6}]\n```'
    assert parse_guesses(fenced, limit=3) == [Guess("Grasz w szachy", 0.6)]
    assert parse_guesses("[]", limit=3) == []


def test_parse_skips_bad_items_and_respects_the_limit():
    raw = """[
        {"fakt": "ok", "pewnosc": 0.5},
        {"fakt": "Masz kota.", "pewnosc": true},
        {"fakt": 7, "pewnosc": 0.5},
        "Masz rower",
        {"text": "Pracujesz zdalnie.", "confidence": 1.7},
        {"fakt": "Grasz na gitarze.", "pewność": -0.2},
        {"fakt": "Biegasz rano.", "pewnosc": 0.4}
    ]"""
    assert parse_guesses(raw, limit=2) == [Guess("Pracujesz zdalnie", 1.0), Guess("Grasz na gitarze", 0.0)]


@pytest.mark.parametrize("raw", ["Nie ma faktów.", '{"fakt": "x"}', "[{'fakt': 'x'}]", "] ["])
def test_parse_rejects_non_lists(raw):
    with pytest.raises(LearnError):
        parse_guesses(raw, limit=3)


def test_list_without_fact_objects_gives_nothing():
    assert parse_guesses('{"a": [1, "x"]}', limit=3) == []


def test_inference_request_contains_only_the_utterance():
    engine = ScriptedEngine(['[{"fakt": "Masz psa o imieniu Burek.", "pewnosc": 0.9}]'])
    guesses = run(infer_facts(engine, "Wyprowadzam Burka na spacer.", limit=3))
    assert guesses == [Guess("Masz psa o imieniu Burek", 0.9)]
    ((system, messages, max_tokens),) = engine.calls
    assert system == EXTRACT_PROMPT and max_tokens == 300
    assert [(m.role, m.content) for m in messages] == [("user", "Wyprowadzam Burka na spacer.")]
    assert CONTEXT_HEADER not in messages[0].content  # bez stanu i znanych faktów


def test_inference_errors():
    with pytest.raises(LearnError):
        run(infer_facts(ScriptedEngine([LLMReply("", "refusal", "m", Usage())]), "x y z", limit=3))
    with pytest.raises(LLMUnavailable):
        run(infer_facts(ScriptedEngine([LLMUnavailable("brak")]), "x y z", limit=3))


def test_extract_prompt_excludes_sensitive_topics():
    for topic in ("zdrowie", "położenie i adres", "pieniądze", "wyznanie", "poglądy polityczne", "orientację"):
        assert topic in EXTRACT_PROMPT


def test_system_prompt_describes_memory_as_it_works():
    p = SYSTEM_PROMPT
    assert "zapamiętuje rozmowy" not in p  # program nie zapisuje całych rozmów (spec, sekcja 12)
    assert "Całych rozmów nie zapamiętujesz" in p
    assert "Przypuszczenia program wyciąga z rozmów" in p
    assert '"zapamiętaj, że ..."' in p and '"co o mnie wiesz"' in p
