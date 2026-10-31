"""Uczenie się w turach rozmowy (docs/spec.md, sekcja 9, "Uczenie się")."""

import asyncio
import json
import logging

import pytest

from host.api.main import create_app
from host.api.service import HostService
from host.brain import Assistant
from host.brain.learn import EXTRACT_PROMPT
from host.core.clock import ManualClock
from host.core.config import Config, LLMConfig
from host.store import Store
from host.tests.brain_helpers import ScriptedEngine
from host.tests.helpers import local_client

LLM_CFG = LLMConfig.model_validate({"engine": "ollama", "model": "m"})
BUREK = json.dumps([{"fakt": "Masz psa o imieniu Burek.", "pewnosc": 0.9}])
NONE = "[]"


def make(replies=("Dobrze.",), *, remote=False, with_assistant=True, **config):
    clock = ManualClock()
    store = Store(clock=clock)
    engine = ScriptedEngine(replies, remote=remote)
    assistant = Assistant(engine, LLM_CFG) if with_assistant else None
    service = HostService(Config.model_validate(config), clock, store, assistant=assistant)
    service.start()
    return service, engine, clock, store


def turn(service, text):
    """Tura rozmowy i dokończenie nauki w tle (wnioski)."""

    async def scenario():
        reply = await service.chat(text)
        await asyncio.gather(*list(service._background))
        return reply

    return asyncio.run(scenario())


def events(store, type_):
    return [e.data for e in store.events(types=[type_])]


def facts(store):
    return [(f.text, f.source, round(f.confidence, 2)) for f in store.facts()]


# --- polecenia bez LLM ---


def test_remember_recall_and_forget_without_the_llm():
    service, engine, _, store = make(with_assistant=False)  # brain.llm.engine = none
    r = turn(service, "Zapamiętaj, że mam psa Burka.")
    assert r == {"text": 'Zapamiętane: "mam psa Burka".', "truncated": False, "model": None,
                 "usage": {"input_tokens": 0, "output_tokens": 0}}
    turn(service, "IRIS, zapisz: pracuję w Gdyni")
    assert facts(store) == [("mam psa Burka", "stated", 1.0), ("pracuję w Gdyni", "stated", 1.0)]
    assert service.core.self_snapshot()["self"]["continuity"]["known_facts"] == 2
    recall = turn(service, "Co o mnie wiesz?")["text"]
    assert recall.startswith("Wiem od ciebie: ") and '"mam psa Burka"' in recall and '"pracuję w Gdyni"' in recall
    assert turn(service, "Zapomnij, że mam psa")["text"] == 'Zapomniane: "mam psa Burka".'
    assert turn(service, "zapomnij o rowerze")["text"] == "Nie znam takiej informacji."
    assert facts(store) == [("pracuję w Gdyni", "stated", 1.0)]
    assert [e["how"] for e in events(store, "fact.learn")] == ["told", "told"]
    assert events(store, "fact.forget") == [{"id": 1, "why": "user"}]


def test_remembering_a_similar_fact_replaces_it():
    service, _, _, store = make(with_assistant=False)
    turn(service, "Zapamiętaj, że pracuję w Gdyni")
    turn(service, "Zapamiętaj, że pracuję w Gdańsku od marca")
    assert len(store.facts()) == 2  # "Gdyni" i "Gdańsku" to inne rdzenie
    turn(service, "zapamiętaj: pracuję w Gdańsku od kwietnia")  # podobieństwo 0,5: nowy fakt
    assert len(store.facts()) == 3
    turn(service, "zapamiętaj: od marca pracuję w Gdańsku zdalnie")  # 0,75: zastępuje
    assert [f.text for f in store.facts()] == [
        "pracuję w Gdyni", "od marca pracuję w Gdańsku zdalnie", "pracuję w Gdańsku od kwietnia",
    ]


def test_forget_everything_needs_a_yes():
    service, _, _, store = make(with_assistant=False)
    for t in ("mam psa Burka", "gram w szachy", "lubię jazz"):
        turn(service, f"zapamiętaj, że {t}")
    assert turn(service, "Zapomnij wszystko")["text"] == (
        "Na pewno usunąć wszystko, co o tobie wiem (3 informacje)? Powiedz tak albo nie."
    )
    assert turn(service, "nie")["text"] == "Dobrze, niczego nie usuwam."
    assert len(store.facts()) == 3
    turn(service, "Zapomnij wszystko")
    assert turn(service, "Tak.")["text"] == "Dobrze, usuwam wszystko, co o tobie wiem."
    assert store.facts() == [] and events(store, "fact.forget")[-1] == {"id": None, "why": "all", "count": 3}
    assert turn(service, "zapomnij wszystko")["text"] == "Nic o tobie nie wiem, nie ma czego usuwać."


# --- wnioski z rozmowy ---


def test_a_guess_starts_weak_and_grows_with_repetition():
    service, engine, _, store = make(["Miłego spaceru.", BUREK, "Pozdrów Burka.", BUREK])
    assert turn(service, "Idę z Burkiem na spacer.")["text"] == "Miłego spaceru."
    system, messages, _ = engine.calls[1]
    assert system == EXTRACT_PROMPT and [m.content for m in messages] == ["Idę z Burkiem na spacer."]
    assert facts(store) == [("Masz psa o imieniu Burek", "inferred", 0.45)]  # start_max: poniżej progu użycia
    turn(service, "Burek dziś szczekał.")
    (f,) = store.facts()
    assert (round(f.confidence, 2), f.reinforced) == (0.65, 1)
    assert [e["how"] for e in events(store, "fact.learn")] == ["inferred", "reinforced"]


def test_guess_used_in_the_context_only_after_reinforcement():
    service, engine, _, store = make(["Ok.", BUREK, "Ok.", BUREK, "Ok.", NONE], learning={"ask": False})
    turn(service, "Idę z Burkiem na spacer.")
    turn(service, "Burek dziś szczekał.")
    assert "Masz psa o imieniu Burek" not in engine.chat_calls[1][1][-1].content  # 0.45: pominięty
    turn(service, "Co robić wieczorem?")
    assert "Masz psa o imieniu Burek (przypuszczenie, pewność 0.7)" in engine.chat_calls[2][1][-1].content


def test_sensitive_and_doubtful_guesses_are_dropped(caplog):
    guesses = json.dumps([
        {"fakt": "Bierzesz leki na ciśnienie.", "pewnosc": 0.9},
        {"fakt": "Mieszkasz przy ulicy Długiej.", "pewnosc": 0.9},
        {"fakt": "Grasz w szachy.", "pewnosc": 0.1},
        {"fakt": "Lubisz jazz.", "pewnosc": 0.7},
    ])
    service, _, _, store = make(["Ok.", guesses], learning={"max_per_turn": 5})
    with caplog.at_level(logging.DEBUG):
        turn(service, "Słucham jazzu po lekach.")
    assert facts(store) == [("Lubisz jazz", "inferred", 0.45)]
    assert "pominięte 3" in caplog.text and "ciśnienie" not in caplog.text


def test_a_guess_matching_a_stated_fact_changes_nothing():
    service, _, _, store = make(["Ok.", json.dumps([{"fakt": "Pracujesz w Gdyni.", "pewnosc": 0.9}])])
    store.add_fact("pracuję w Gdyni", "stated")
    turn(service, "Jadę do pracy.")
    assert facts(store) == [("pracuję w Gdyni", "stated", 1.0)]


@pytest.mark.parametrize(
    "remote, infer, expected",
    [(False, "local_only", True), (True, "local_only", False), (True, "always", True), (False, "never", False)],
)
def test_inference_policy(remote, infer, expected):
    service, engine, _, store = make(["Ok.", BUREK], remote=remote, learning={"infer": infer})
    turn(service, "Idę z Burkiem na spacer.")
    assert (len(engine.calls) == 2) is expected and (len(store.facts()) == 1) is expected


def test_inference_errors_leave_the_turn_alone(caplog):
    service, _, _, store = make(["Odpowiedź.", "to nie jest JSON z Burkiem"])
    with caplog.at_level(logging.INFO):
        assert turn(service, "Idę z Burkiem na spacer.")["text"] == "Odpowiedź."
    assert store.facts() == [] and "wnioski z rozmowy pominięte (LearnError)" in caplog.text
    assert "Burk" not in caplog.text


def test_learning_can_be_switched_off():
    service, engine, _, store = make(["Odpowiedź LLM."], learning={"enabled": False})
    assert turn(service, "zapamiętaj, że mam psa")["text"] == "Odpowiedź LLM."  # trafia do LLM
    assert store.facts() == [] and len(engine.calls) == 1


# --- pytania ---


def asked_service(**config):
    """Serwis z przypuszczeniem o psie i pytaniem o nie dopisanym do odpowiedzi."""
    service, engine, clock, store = make(["Miłego spaceru.", BUREK, "Ładna pogoda.", NONE], **config)
    turn(service, "Idę z Burkiem na spacer.")
    reply = turn(service, "Jaka pogoda?")["text"]
    return service, engine, clock, store, reply


def test_iris_asks_about_a_guess_and_yes_confirms_it():
    service, engine, _, store, reply = asked_service()
    assert reply == "Ładna pogoda. Czy dobrze rozumiem: masz psa o imieniu Burek?"
    (f,) = store.facts()
    assert f.asked_at is not None and events(store, "fact.ask") == [{"id": f.id}]
    calls = len(engine.calls)
    assert turn(service, "Tak, zgadza się.")["text"] == "Dobrze, zapamiętane."
    assert len(engine.calls) == calls  # odpowiedź obsłużył host
    assert facts(store) == [("Masz psa o imieniu Burek", "stated", 1.0)]
    assert events(store, "fact.confirm") == [{"id": f.id}]


def test_no_with_a_correction_replaces_the_guess():
    service, _, _, store, _ = asked_service()
    assert turn(service, "Nie, mam kota Filemona.")["text"] == 'Dobrze, poprawione: "mam kota Filemona".'
    assert facts(store) == [("mam kota Filemona", "stated", 1.0)]
    assert events(store, "fact.forget")[-1]["why"] == "denied"
    assert events(store, "fact.learn")[-1]["how"] == "corrected"


def test_plain_no_removes_the_guess():
    service, _, _, store, _ = asked_service()
    assert turn(service, "nie")["text"] == "Dobrze, usuwam to przypuszczenie."
    assert store.facts() == []


def test_other_utterance_closes_the_question():
    service, engine, _, store, _ = asked_service()
    engine._replies = ["O 18.", NONE, "O 18.", NONE]
    assert turn(service, "Która godzina?")["text"] == "O 18."
    assert turn(service, "tak")["text"] == "O 18."  # pytanie zamknięte: "tak" idzie do LLM
    assert facts(store) == [("Masz psa o imieniu Burek", "inferred", 0.45)]


def test_answer_after_the_window_goes_to_the_llm():
    service, engine, clock, store, _ = asked_service()
    clock.advance(121)
    engine._replies = ["Co tak?", NONE]
    assert turn(service, "tak")["text"] == "Co tak?"
    assert store.facts()[0].source == "inferred"


def test_questions_respect_the_interval_and_quiet_mode():
    service, engine, clock, store, _ = asked_service()
    engine._replies = ["Ok.", NONE]
    turn(service, "Która godzina?")  # zamyka pytanie
    assert "Czy dobrze rozumiem" not in turn(service, "A jutro?")["text"]  # minęło mniej niż 900 s
    store.add_fact("Lubisz jazz", "inferred", 0.6)
    clock.advance(901)
    assert turn(service, "A teraz?")["text"].endswith("Czy dobrze rozumiem: lubisz jazz?")
    quiet, _, _, _, reply = asked_service(initiative={"quiet": True})
    assert reply == "Ładna pogoda."


# --- zanikanie, REST i prywatność ---


def test_unreinforced_guesses_decay_on_tick():
    service, _, clock, store = make(["Ok."])
    weak = store.add_fact("Grasz w szachy", "inferred", 0.25)
    mid = store.add_fact("Lubisz jazz", "inferred", 0.6)
    store.add_fact("mam psa", "stated")
    clock.advance(31 * 86400)
    service.tick()
    assert facts(store) == [("Lubisz jazz", "inferred", 0.5), ("mam psa", "stated", 1.0)]
    assert events(store, "fact.forget") == [{"id": weak.id, "why": "decay"}]
    service.tick()  # raz na dobę
    assert store.get_fact(mid.id).confidence == pytest.approx(0.5)


def test_rest_delete_records_the_forget_event():
    clock = ManualClock()
    store = Store(clock=clock)
    app = create_app(Config(), clock=clock, store=store, run_tick_loop=False, token=None)
    with local_client(app) as client:
        f = store.add_fact("mam psa", "stated")
        assert client.delete(f"/facts/{f.id}").status_code == 204
        assert client.delete(f"/facts/{f.id}").status_code == 404
    assert events(store, "fact.forget") == [{"id": f.id, "why": "user"}]


def test_fact_text_never_reaches_events_or_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        service, _, _, store, _ = asked_service()
        turn(service, "Nie, mam kota Filemona.")
        turn(service, "zapamiętaj, że gram na wiolonczeli")
        turn(service, "co o mnie wiesz")
    everything = json.dumps([e.data for e in store.events()], ensure_ascii=False)
    for secret in ("Burek", "Filemon", "wiolonczel", "spacer"):
        assert secret not in everything and secret not in caplog.text, secret


def test_rest_facts_say_which_reach_the_llm():
    for remote, send_facts, expected in [(False, "local_only", [True, True, False]),
                                         (True, "local_only", [False, False, False]),
                                         (True, "always", [True, True, False])]:
        clock = ManualClock()
        store = Store(clock=clock)
        engine = ScriptedEngine(["Ok."], remote=remote)
        cfg = LLMConfig.model_validate({"engine": "ollama", "model": "m", "send_facts": send_facts})
        app = create_app(Config(), clock=clock, store=store, run_tick_loop=False, token=None,
                         assistant=Assistant(engine, cfg))
        with local_client(app) as client:
            store.add_fact("mam psa", "stated")
            store.add_fact("Lubisz jazz", "inferred", 0.6)
            store.add_fact("Grasz w szachy", "inferred", 0.45)
            got = [f["in_context"] for f in client.get("/facts").json()["facts"]]
        assert got == expected, (remote, send_facts)


# --- reakcje użytkownika: nagroda dla przypuszczeń użytych w odpowiedzi ---


def _service_with_used_guess(**config):
    """Przypuszczenie o pewności 0,65 (powyżej progu użycia), więc trafia do kontekstu lokalnego modelu."""
    service, engine, clock, store = make(["Dobrze.", NONE], learning={"ask": False, **config})
    store.add_fact("Masz psa o imieniu Burek", "inferred", 0.65)
    return service, engine, store


def test_praise_strengthens_the_guesses_used_in_the_last_answer_without_the_llm():
    service, engine, store = _service_with_used_guess()
    turn(service, "Co słychać?")  # odpowiedź LLM z przypuszczeniem w kontekście
    calls = len(engine.calls)
    reply = turn(service, "Świetnie!")
    assert reply["text"] == "Dobrze, zapisuję, że to było trafne." and reply["model"] is None
    assert len(engine.calls) == calls  # reakcję obsługuje host, bez modelu
    (f,) = store.facts()
    assert (round(f.confidence, 2), f.reinforced) == (0.75, 1)
    assert [e["how"] for e in events(store, "fact.learn")] == ["praised"]
    assert "Burek" not in json.dumps(events(store, "fact.learn"))
    # reakcja zużywa listę: druga pochwała idzie do modelu jak zwykła rozmowa
    turn(service, "dzięki")
    assert len(engine.calls) > calls
    assert round(store.facts()[0].confidence, 2) == 0.75


def test_criticism_weakens_the_guess_and_a_weak_guess_is_no_longer_judged():
    service, engine, store = _service_with_used_guess()
    turn(service, "Co słychać?")
    assert turn(service, "To nie tak.")["text"] == "Dobrze, osłabiam przypuszczenia użyte w tej odpowiedzi."
    assert round(store.facts()[0].confidence, 2) == 0.45
    assert [e["how"] for e in events(store, "fact.learn")] == ["criticized"]
    turn(service, "Jak się masz?")  # 0,45 jest poniżej progu użycia (0,5): nie trafia do kontekstu
    assert turn(service, "źle")["model"] is not None  # nie było czego oceniać, więc zwykła rozmowa
    assert round(store.facts()[0].confidence, 2) == 0.45


def test_a_criticism_below_the_forget_threshold_removes_the_guess():
    service, engine, clock, store = make(["Ok.", NONE], learning={"ask": False, "feedback_down": 0.4})
    store.add_fact("Masz psa o imieniu Burek", "inferred", 0.55)
    turn(service, "Co słychać?")
    assert turn(service, "to nieprawda")["model"] is None  # 0,55 - 0,4 = 0,15 < forget_below (0,2)
    assert store.facts() == []
    forgotten = events(store, "fact.forget")
    assert forgotten[-1]["why"] == "criticized" and "Burek" not in json.dumps(forgotten)


def test_stated_facts_ignore_reactions_and_feedback_can_be_switched_off():
    service, engine, clock, store = make(["Ok.", NONE])
    store.add_fact("Mieszkasz w Gdyni", "stated")
    turn(service, "Hej")
    assert turn(service, "dzięki")["model"] is not None  # fakt stated: nie było czego oceniać
    assert store.facts()[0].reinforced == 0

    service, engine, store = _service_with_used_guess(feedback=False)
    turn(service, "Co słychać?")
    assert turn(service, "dzięki")["model"] is not None
    assert round(store.facts()[0].confidence, 2) == 0.65
