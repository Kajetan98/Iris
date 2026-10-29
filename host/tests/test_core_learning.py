"""Reguły uczenia się (docs/spec.md, sekcja 9, "Uczenie się"): polecenia, podobieństwo, pytania."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from host.core.config import Config, LearningConfig
from host.core.learning import (
    MemoryCommand,
    best_match,
    is_sensitive,
    parse_answer,
    parse_memory,
    parse_feedback,
    pick_question,
    question_for,
    recall_text,
    similarity,
)
from host.store.models import Fact

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def fact(id_, text, source="stated", confidence=1.0, *, age_days=0, asked_days=None):
    at = T0 - timedelta(days=age_days)
    asked = None if asked_days is None else T0 - timedelta(days=asked_days)
    return Fact(id_, text, source, confidence, at, at, 1, 0, asked)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Zapamiętaj, że mam psa Burka.", MemoryCommand("remember", "mam psa Burka")),
        ("zapamiętaj mam psa Burka", MemoryCommand("remember", "mam psa Burka")),
        ("IRIS, zapisz sobie: pracuję w szpitalu", MemoryCommand("remember", "pracuję w szpitalu")),
        ("Pamiętaj, że w piątki gram w kosza", MemoryCommand("remember", "w piątki gram w kosza")),
        ('Zapamiętaj o tym, że "lubię kawę"', MemoryCommand("remember", "lubię kawę")),
        ("Zapomnij, że mam psa", MemoryCommand("forget", "mam psa")),
        ("zapomnij o kawie", MemoryCommand("forget", "kawie")),
        ("Zapomnij wszystko.", MemoryCommand("forget_all")),
        ("zapomnij o mnie wszystko", MemoryCommand("forget_all")),
        ("Co o mnie wiesz?", MemoryCommand("recall")),
        ("IRIS, co wiesz o mnie", MemoryCommand("recall")),
        ("Czego się o mnie nauczyłaś?", MemoryCommand("recall")),
        ("czego się o mnie nauczyłeś", MemoryCommand("recall")),
    ],
)
def test_memory_commands(text, expected):
    assert parse_memory(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Pamiętaj o zakupach",  # przypomnienie, nie fakt: "pamiętaj" wymaga "że"
        "Co wiesz o pogodzie?",
        "zapamiętaj to",  # za krótki fakt
        "Czy zapamiętasz, że mam psa?",
        "Jak się masz",
        "przełącz na laptop",
    ],
)
def test_not_memory_commands(text):
    assert parse_memory(text) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Tak", (True, "")),
        ("tak, zgadza się", (True, "")),
        ("Zgadza się.", (True, "")),
        ("Dokładnie!", (True, "")),
        ("Nie", (False, "")),
        ("Nieprawda.", (False, "")),
        ("nie zgadza się", (False, "")),
        ("Nie, mam kota.", (False, "mam kota")),
        ("nie - pracuję zdalnie", (False, "pracuję zdalnie")),
        ("Niedziela jest wolna", (None, "")),  # "nie" tylko jako osobne słowo
        ("Taksówka przyjedzie", (None, "")),
        ("Jaka jest pogoda?", (None, "")),
    ],
)
def test_answers(text, expected):
    assert parse_answer(text) == expected


def test_similarity_follows_polish_endings():
    assert similarity("Pracujesz w Krakowie.", "pracuję w krakowie") == 1.0
    assert similarity("Lubisz kawę z mlekiem.", "Lubisz kawa z mlekiem") == 1.0
    assert similarity("Masz psa o imieniu Burek.", "Masz kota.") == 0.0
    assert similarity("o 18:00 trening", "trening o 18:00") == 1.0
    assert similarity("", "coś") == 0.0


def test_best_match_prefers_the_closest_then_the_newest():
    facts = [fact(1, "Mam psa Burka"), fact(2, "Mam kota Filemona"), fact(3, "Mam psa Azora", age_days=3)]
    assert best_match("mam psa", facts, 0.5).id == 1  # psa: Burka (nowszy) i Azora mają ten sam wynik
    assert best_match("kot Filemon", facts, 0.5).id == 2
    assert best_match("lubię rower", facts, 0.5) is None


def test_sensitive_words():
    stems = Config().learning.sensitive_words
    for text in ("Bierzesz leki na ciśnienie.", "Mieszkasz przy ulicy Długiej.", "Zarabiasz dobrze.",
                 "Chodzisz do lekarza we wtorki.", "Jesteś katolikiem."):
        assert is_sensitive(text, stems), text
    for text in ("Lubisz długo spać.", "Masz lekki rower.", "Pracujesz zdalnie.", "Grasz w szachy."):
        assert not is_sensitive(text, stems), text


def test_question_text():
    assert question_for(fact(1, "Masz psa o imieniu Burek.", "inferred", 0.6)) == (
        "Czy dobrze rozumiem: masz psa o imieniu Burek?"
    )
    assert question_for(fact(1, "USA to twój ulubiony kraj", "inferred", 0.6)).startswith("Czy dobrze rozumiem: USA")


def test_pick_question():
    cfg = LearningConfig()
    facts = [
        fact(1, "Lubisz kawę", "inferred", 0.45),
        fact(2, "Masz psa", "inferred", 0.65),
        fact(3, "Grasz w szachy", "inferred", 0.85, asked_days=2),  # pytano niedawno
        fact(4, "Pracujesz zdalnie", "stated", 1.0),  # pewny, nie ma o co pytać
        fact(5, "Jeździsz rowerem", "inferred", 0.3),  # poniżej ask_min
    ]
    assert pick_question(facts, T0, cfg).id == 2
    facts[2] = fact(3, "Grasz w szachy", "inferred", 0.85, asked_days=8)  # minęło ask_again_days
    assert pick_question(facts, T0, cfg).id == 3
    assert pick_question([facts[3], facts[4]], T0, cfg) is None


def test_recall_text():
    assert recall_text([], 0.5) == "Jeszcze nic o tobie nie wiem. Możesz powiedzieć: zapamiętaj, że ..."
    facts = [fact(i, f"fakt numer {i}") for i in range(1, 8)]
    facts += [fact(10, "Lubisz kawę.", "inferred", 0.65), fact(11, "Masz psa.", "inferred", 0.3)]
    text = recall_text(facts, 0.5)
    assert text.startswith('Wiem od ciebie: "fakt numer 1"; "fakt numer 2"')
    assert "Przypuszczam, że lubisz kawę (65%)." in text
    assert "psa" not in text  # poniżej progu użycia
    assert text.endswith("Oprócz tego 3 informacje, pełna lista jest w aplikacji.")
    confirmed = [fact(1, "Masz psa o imieniu Burek", "stated"), fact(2, "USA to twój ulubiony kraj", "stated")]
    assert recall_text(confirmed, 0.5) == 'Wiem od ciebie: "masz psa o imieniu Burek"; "USA to twój ulubiony kraj".'
    only_weak = [fact(11, "Masz psa.", "inferred", 0.3)]
    assert recall_text(only_weak, 0.5) == "Mam tylko 1 niepewne przypuszczenie, pełna lista jest w aplikacji."


def test_learning_config_order():
    with pytest.raises(ValidationError, match="forget_below < start_max"):
        LearningConfig(start_max=0.1, forget_below=0.2)
    with pytest.raises(ValidationError):
        LearningConfig(start_max=0.95, max_inferred=0.9)
    assert Config.model_validate({"learning": {"infer": "never"}}).learning.infer == "never"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Dobrze", "praise"), ("dobrze.", "praise"), ("Świetnie!", "praise"), ("dobra odpowiedź", "praise"),
        ("Trafnie", "praise"), ("dokładnie tak", "praise"), ("Dzięki", "praise"), ("dziękuję!", "praise"),
        ("IRIS, dzięki", "praise"), ("  DZIĘKI  ", "praise"),
        ("Źle", "criticism"), ("To nie tak.", "criticism"), ("nie tak", "criticism"), ("Nie o to pytałem", "criticism"),
        ("nie o to chodziło!", "criticism"), ("to nieprawda", "criticism"), ("Mylisz się", "criticism"),
        ("iris, to nie tak", "criticism"),
        # zwykła rozmowa: słowo reakcji na początku dłuższej wypowiedzi to nie reakcja
        ("źle się czuję", None), ("dobrze, ale krócej", None), ("dzięki za pogodę", None), ("nie", None), ("tak", None), ("", None),
    ],
)
def test_feedback_is_the_whole_utterance(text, expected):
    assert parse_feedback(text) == expected


def test_feedback_settings_have_sane_defaults():
    cfg = LearningConfig()
    assert cfg.feedback and (cfg.feedback_up, cfg.feedback_down) == (0.1, 0.2)
    with pytest.raises(ValidationError):
        LearningConfig(feedback_up=0)
