import asyncio
import json
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from host.api.main import create_app
from host.api.service import ChatDisabled, ChatError, HostService
from host.brain import Assistant, LLMConfigError, LLMError, LLMRefusal, LLMReply, LLMUnavailable
from host.core.clock import ManualClock
from host.core.config import Config, LLMConfig
from host.store import Store
from host.tests.brain_helpers import ScriptedEngine
from host.tests.helpers import heartbeat, make_event, local_client

LLM_CFG = LLMConfig.model_validate({"engine": "ollama", "model": "m"})


def make(replies=("Dobrze.",), *, remote=False, with_assistant=True):
    clock = ManualClock()
    store = Store(clock=clock)
    engine = ScriptedEngine(replies, remote=remote)
    service = HostService(
        Config(), clock, store, assistant=Assistant(engine, LLM_CFG) if with_assistant else None
    )
    service.start()
    return service, engine, clock, store


def run(coro):
    return asyncio.run(coro)


def user_turn(engine, call=-1):
    return engine.chat_calls[call][1][-1].content


# --- tura rozmowy ---


def test_chat_returns_answer_and_sends_state():
    service, engine, _, _ = make(["Jest południe."])
    service.core.handle_event(heartbeat(18))
    result = run(service.chat("  Która godzina?  "))
    assert result == {
        "text": "Jest południe.",
        "truncated": False,
        "model": "scripted",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    turn = user_turn(engine)
    assert "Tryb pracy: normalny" in turn and "bateria: brak danych → 18%" in turn
    assert turn.endswith("[WYPOWIEDŹ UŻYTKOWNIKA]\nKtóra godzina?")
    assert "Czas: czwartek, 1 stycznia 2026, 12:00" in turn


def test_chat_counts_as_interaction_and_ends_long_silence():
    service, _, clock, store = make()
    clock.advance(4 * 3600 + 1)
    service.tick()
    assert "long_silence" in service.core.drives.active()
    run(service.chat("Cześć"))
    assert "long_silence" not in service.core.drives.active()
    exits = [e.data["drive"] for e in store.events(types=["drive.exit"])]
    assert exits == ["long_silence"]


def test_failed_chat_still_counts_as_interaction():
    service, _, clock, _ = make([LLMUnavailable("brak")])
    clock.advance(4 * 3600 + 1)
    service.tick()
    with pytest.raises(ChatError):
        run(service.chat("Cześć"))
    assert "long_silence" not in service.core.drives.active()


# --- cofanie wycinka ---


def test_failed_chat_rolls_the_slice_back():
    service, engine, _, _ = make([LLMUnavailable("brak połączenia"), "Już działa."])
    service.core.handle_event(heartbeat(71))
    with pytest.raises(ChatError) as e:
        run(service.chat("Jak bateria?"))
    assert e.value.kind == "unavailable"
    run(service.chat("Jak bateria?"))
    first, second = user_turn(engine, 0), user_turn(engine, 1)
    assert "bateria: brak danych → 71%" in first
    assert first == second  # te same zmiany stanu w obu próbach


def test_successful_chat_does_not_repeat_changes():
    service, engine, _, _ = make(["a", "b"])
    service.core.handle_event(heartbeat(71))
    run(service.chat("raz"))
    run(service.chat("dwa"))
    assert "Zmiany stanu ciała" in user_turn(engine, 0)
    assert "Zmiany stanu ciała" not in user_turn(engine, 1)


def test_cancelled_chat_rolls_the_slice_back():
    async def scenario():
        service, engine, _, _ = make(["a", "b"])
        service.core.handle_event(heartbeat(71))
        gate = asyncio.Event()
        original = engine.complete

        async def hang(system, messages, *, max_tokens):
            engine.calls.append((system, list(messages), max_tokens))
            await gate.wait()

        engine.complete = hang
        task = asyncio.create_task(service.chat("wisi"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        engine.complete = original
        await service.chat("jeszcze raz")
        assert user_turn(engine, 0).split("[WYPOWIEDŹ")[0] == user_turn(engine, 1).split("[WYPOWIEDŹ")[0]

    run(scenario())


@pytest.mark.parametrize(
    "error, kind",
    [
        (LLMUnavailable("x"), "unavailable"),
        (LLMError("x"), "failed"),
        (LLMConfigError("x"), "failed"),
        (LLMReply("", "refusal", "m"), "refused"),
        ("   ", "failed"),  # pusta odpowiedź
    ],
)
def test_error_kinds(error, kind):
    service, _, _, _ = make([error])
    with pytest.raises(ChatError) as e:
        run(service.chat("q"))
    assert e.value.kind == kind


def test_unexpected_exception_propagates_after_rollback():
    service, engine, _, _ = make([RuntimeError("bug"), "ok"])
    service.core.handle_event(heartbeat(71))
    with pytest.raises(RuntimeError):
        run(service.chat("q"))
    run(service.chat("q"))
    assert user_turn(engine, 0) == user_turn(engine, 1)


# --- wyłączony LLM i walidacja ---


def test_disabled_llm_raises_and_does_not_count_as_interaction():
    service, _, clock, _ = make(with_assistant=False)
    clock.advance(4 * 3600 + 1)
    service.tick()
    with pytest.raises(ChatDisabled):
        run(service.chat("Cześć"))
    assert "long_silence" in service.core.drives.active()


def test_blank_text_is_rejected_before_anything_happens():
    service, engine, clock, _ = make()
    clock.advance(4 * 3600 + 1)
    service.tick()
    with pytest.raises(ValueError):
        run(service.chat("   "))
    assert engine.calls == [] and "long_silence" in service.core.drives.active()


# --- kolejność tur ---


def test_chats_are_serialized_and_slice_is_not_duplicated():
    async def scenario():
        service, engine, _, _ = make(["a", "b"])
        service.core.handle_event(heartbeat(71))
        original = engine.complete

        async def slow(system, messages, *, max_tokens):
            await asyncio.sleep(0.05)
            return await original(system, messages, max_tokens=max_tokens)

        engine.complete = slow
        await asyncio.gather(service.chat("pierwsza"), service.chat("druga"))
        turns = [user_turn(engine, i) for i in range(2)]
        assert sum("Zmiany stanu ciała" in t for t in turns) == 1

    run(scenario())


# --- fakty i prywatność ---


def test_facts_reach_a_local_engine_but_not_a_remote_one():
    for remote, expected in [(False, True), (True, False)]:
        service, engine, _, store = make(remote=remote)
        store.add_fact("Mieszkam w Gdańsku", "stated")
        run(service.chat("q"))
        assert ("Mieszkam w Gdańsku" in user_turn(engine)) is expected


def test_chat_survives_a_failing_facts_query(monkeypatch, caplog):
    service, engine, _, store = make()

    def boom():
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store, "facts", boom)
    with caplog.at_level(logging.ERROR):
        assert run(service.chat("q"))["text"] == "Dobrze."
    assert "Znane fakty" not in user_turn(engine)


def test_conversation_text_is_not_stored_or_logged(caplog):
    service, _, _, store = make(["ODPOWIEDZ-TAJNA-77"])
    with caplog.at_level(logging.DEBUG):
        run(service.chat("PYTANIE-TAJNE-42"))
    dump = []
    for table in ("sessions", "events", "state_snapshots", "facts"):
        dump += [str(row) for row in store._conn.execute(f"SELECT * FROM {table}")]
    haystack = "\n".join(dump) + "\n".join(r.getMessage() for r in caplog.records)
    assert "PYTANIE-TAJNE-42" not in haystack and "ODPOWIEDZ-TAJNA-77" not in haystack


# --- REST ---


@pytest.fixture
def chat_client():
    clock = ManualClock()
    engine = ScriptedEngine(["Pierwsza.", "Druga."])
    app = create_app(Config(), clock, Store(clock=clock), Assistant(engine, LLM_CFG), run_tick_loop=False)
    with local_client(app) as c:
        c.engine = engine
        yield c


def test_post_chat_ok_and_history(chat_client):
    r1 = chat_client.post("/chat", json={"text": "Cześć"})
    assert r1.status_code == 200
    assert r1.json() == {
        "text": "Pierwsza.", "truncated": False, "model": "scripted",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    r2 = chat_client.post("/chat", json={"text": "A teraz?"})
    assert r2.json()["text"] == "Druga."
    history = chat_client.engine.chat_calls[1][1]
    assert [(m.role, m.content[:6]) for m in history[:2]] == [("user", "Cześć"), ("assistant", "Pierws")]


@pytest.mark.parametrize(
    "payload",
    [{}, {"text": ""}, {"text": "   "}, {"text": "x" * 4001}, {"text": "a", "extra": 1}, {"text": 5}, {"text": None}],
)
def test_post_chat_rejects_bad_requests(chat_client, payload):
    assert chat_client.post("/chat", json=payload).status_code == 422
    assert chat_client.engine.calls == []


def test_post_chat_accepts_max_length(chat_client):
    assert chat_client.post("/chat", json={"text": "x" * 4000}).status_code == 200


def test_post_chat_501_when_llm_disabled():
    app = create_app(Config(), ManualClock(), run_tick_loop=False)
    with local_client(app) as c:
        r = c.post("/chat", json={"text": "Cześć"})
    assert r.status_code == 501 and "wyłączony" in r.json()["detail"]


@pytest.mark.parametrize(
    "error, status",
    [
        (LLMUnavailable("brak połączenia"), 503),
        (LLMError("zły format"), 502),
        (LLMConfigError("brak modelu"), 502),
        (LLMReply("", "refusal", "m"), 422),
    ],
)
def test_post_chat_error_statuses(error, status):
    clock = ManualClock()
    app = create_app(Config(), clock, Store(clock=clock), Assistant(ScriptedEngine([error]), LLM_CFG), run_tick_loop=False)
    with local_client(app) as c:
        r = c.post("/chat", json={"text": "q"})
    assert r.status_code == status and "silnik LLM" in r.json()["detail"]


def test_app_builds_and_closes_assistant_from_config(monkeypatch):
    engine = ScriptedEngine(["Z konfiguracji."])
    built = []

    def fake_build(cfg):
        built.append(cfg)
        return Assistant(engine, cfg)

    monkeypatch.setattr("host.brain.build_assistant", fake_build)
    cfg = Config.model_validate({"brain": {"llm": {"engine": "ollama", "model": "bielik"}}})
    app = create_app(cfg, ManualClock(), run_tick_loop=False)
    with local_client(app) as c:
        assert c.post("/chat", json={"text": "q"}).json()["text"] == "Z konfiguracji."
    assert built[0].model == "bielik" and engine.closed


def test_injected_assistant_is_not_closed_by_the_app():
    clock = ManualClock()
    engine = ScriptedEngine()
    app = create_app(Config(), clock, Store(clock=clock), Assistant(engine, LLM_CFG), run_tick_loop=False)
    with local_client(app):
        pass
    assert not engine.closed


def test_disabled_llm_never_loads_brain_modules():
    code = (
        "import sys\n"
        "from fastapi.testclient import TestClient\n"
        "from host.api.main import create_app\n"
        "from host.core.config import Config\n"
        "from host.store import Store\n"
        "app = create_app(Config.model_validate({'store': {'path': ':memory:'}}), run_tick_loop=False)\n"
        "with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 50000)) as c:\n"
        "    assert c.post('/chat', json={'text': 'Cześć'}).status_code == 501\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('host.brain'))\n"
        "assert not loaded, loaded\n"
    )
    root = Path(__file__).parents[2]
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
