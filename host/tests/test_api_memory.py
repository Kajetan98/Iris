"""Pamięć IRIS w hoście: polecenia, nauka z rozmowy, narzędzia, reakcje, REST i strumień
(docs/spec.md, sekcje 9, 10 i 12)."""

from __future__ import annotations

import asyncio
import json
import dataclasses
import sqlite3

import pytest
from starlette.websockets import WebSocketDisconnect

from host.api.main import create_app
from host.api.service import ChatDisabled, HostService
from host.brain import Assistant
from host.brain.learn import CONCEPTS_PROMPT
from host.brain.llm import LLMReply, ToolCall, Usage
from host.core.clock import ManualClock
from host.core.config import Config, LLMConfig
from host.store import Store
from host.tests.brain_helpers import ScriptedEngine
from host.tests.helpers import local_client

LLM_CFG = LLMConfig.model_validate({"engine": "ollama", "model": "m"})
TOKEN = "token-testowy-1234567890"
LAMPA = json.dumps({"domain": "dom", "concepts": ["lampa", "salon"]})


class ToolEngine(ScriptedEngine):
    """Silnik z narzędziami: zapisuje oferowane narzędzia przy każdym zapytaniu."""

    supports_tools = True

    def __init__(self, replies, *, remote=False):
        super().__init__(replies, remote=remote)
        self.offered = []

    async def complete(self, system, messages, *, max_tokens, tools=(), allow_calls=True):
        self.offered.append([t.name for t in tools] if allow_calls else [])
        return await super().complete(system, messages, max_tokens=max_tokens)


def make(replies=("Dobrze.",), *, remote=False, engine=None, with_assistant=True, **config):
    clock = ManualClock()
    cfg = Config.model_validate(config)
    store = Store(clock=clock, memory=cfg.memory)
    engine = engine or ScriptedEngine(replies, remote=remote)
    assistant = Assistant(engine, LLM_CFG if not config.get("brain") else cfg.brain.llm) if with_assistant else None
    service = HostService(cfg, clock, store, assistant=assistant)
    service.start()
    return service, engine, clock, store


def turn(service, text):
    """Tura rozmowy i dokończenie nauki w tle."""

    async def scenario():
        reply = await service.chat(text)
        await service.memory.drain()
        return reply

    return asyncio.run(scenario())


def labels(store):
    return sorted(c.label for c in store.concepts().values())


def events(store, type_):
    return [e.data for e in store.events(types=[type_])]


# --- polecenia bez LLM ---


def test_remember_recall_forget_without_the_llm():
    service, _, _, store = make(with_assistant=False)
    r = turn(service, "Zapamiętaj, że lampa stoi w salonie.")
    assert r == {"text": "Zapamiętane: lampa, stoi, salonie.", "truncated": False, "model": None,
                 "usage": {"input_tokens": 0, "output_tokens": 0}}
    assert labels(store) == ["lampa", "salonie", "stoi"]
    assert events(store, "memory.learn") == [{"how": "told", "fresh": 3, "reinforced": 0, "domain": "dom"}]
    recall = turn(service, "co o mnie wiesz?")["text"]
    assert recall.startswith("Najmocniej kojarzę: ") and recall.endswith("Razem pamiętam 3 pojęcia.")
    assert turn(service, "Zapomnij o lampie")["text"] == "Zapomniane: lampa."
    assert turn(service, "Zapomnij o kosmosie")["text"] == "Nie pamiętam takiego pojęcia."
    assert service.core.self_snapshot()["self"]["continuity"]["known_concepts"] == 2
    assert events(store, "memory.forget") == [{"why": "user", "count": 1}]


def test_remember_keeps_sensitive_concepts_but_never_numbers():
    service, _, _, store = make(with_assistant=False)
    assert turn(service, "zapamiętaj, że mam cukrzycę i PIN 1234")["text"] == "Zapamiętane: cukrzycę, PIN."
    assert turn(service, "zapamiętaj, że 12 34")["text"] == "Nie znalazłem w tym zdaniu niczego do zapamiętania."
    assert labels(store) == ["PIN", "cukrzycę"]


def test_forget_all_asks_first_and_answers_close_the_question():
    service, _, clock, store = make(with_assistant=False)
    assert turn(service, "zapomnij wszystko")["text"] == "Nic nie pamiętam, nie ma czego usuwać."
    turn(service, "zapamiętaj, że jutro mam spotkanie")
    assert turn(service, "zapomnij wszystko")["text"] == (
        "Na pewno usunąć całą pamięć (2 pojęcia)? Powiedz tak albo nie.")
    assert turn(service, "nie")["text"] == "Dobrze, niczego nie usuwam."
    assert store.concept_count() == 2
    turn(service, "zapomnij wszystko")
    clock.advance(121)  # po answer_window_s "tak" jest zwykłą wypowiedzią: trafia do LLM, którego tu nie ma
    with pytest.raises(ChatDisabled):
        turn(service, "tak")
    assert store.concept_count() == 2
    turn(service, "zapomnij wszystko")
    assert turn(service, "Tak, usuń.")["text"] == "Pamięć wyczyszczona."
    assert store.concept_count() == 0 and not store.has_facts_v3()
    assert events(store, "memory.forget")[-1] == {"why": "all", "count": 2}


def test_memory_disabled_sends_commands_to_the_llm():
    service, engine, _, store = make(memory={"enabled": False})
    turn(service, "zapamiętaj, że lubię kawę")
    assert len(engine.chat_calls) == 1 and store.concept_count() == 0
    assert not [c for c in engine.calls if c[0] == CONCEPTS_PROMPT]


# --- nauka z rozmowy ---


def test_conversation_learns_concepts_from_the_llm_in_the_background():
    service, engine, _, store = make(["Dobrze.", LAMPA])
    r = turn(service, "Zapal lampę w salonie")
    assert r["text"] == "Dobrze." and r["model"] == "scripted"
    extraction = [c for c in engine.calls if c[0] == CONCEPTS_PROMPT]
    assert len(extraction) == 1 and [m.content for m in extraction[0][1]] == ["Zapal lampę w salonie"]
    assert labels(store) == ["lampa", "salon"]
    assert events(store, "memory.learn") == [
        {"how": "conversation", "fresh": 2, "reinforced": 0, "domain": "dom", "source": "llm"}]
    # Pojęcia nie trafiają do zdarzeń.
    assert "lampa" not in json.dumps([e.data for e in store.events()])


def test_bad_llm_reply_falls_back_to_the_heuristic():
    service, _, _, store = make(["Dobrze.", "nie wiem"])
    turn(service, "Mój pies Burek lubi spacery")
    assert labels(store) == ["burek", "lubi", "pies", "spacery"]
    assert events(store, "memory.learn")[0]["source"] == "heuristic"


def test_remote_engine_with_local_only_infer_uses_only_the_heuristic():
    service, engine, _, store = make(["Dobrze."], remote=True)
    turn(service, "Zapal lampę w salonie")
    assert not [c for c in engine.calls if c[0] == CONCEPTS_PROMPT]
    assert labels(store) == ["lampę", "salonie", "zapal"]


def test_sensitive_utterance_does_not_teach_the_graph():
    service, engine, _, store = make(["Dobrze.", LAMPA])
    turn(service, "Mieszkam na ulicy Polnej w Krakowie")
    assert store.concept_count() == 0 and not [c for c in engine.calls if c[0] == CONCEPTS_PROMPT]


def test_llm_concepts_with_sensitive_words_or_numbers_are_dropped():
    service, _, _, store = make(["Dobrze.", json.dumps({"domain": "ty", "concepts": ["tętno", "rower", "Rower 3"]})])
    turn(service, "Jeżdżę na rowerze")
    assert labels(store) == ["rower"]


# --- narzędzia i reakcje ---


def tool_call(name, **args):
    return LLMReply("", "tool_use", "scripted", Usage(1, 1), tool_calls=(ToolCall("t1", name, args),))


def test_recall_tool_answers_from_the_graph_and_praise_reinforces_those_concepts():
    engine = ToolEngine([tool_call("iris_recall", query="lampa"), "Pamiętam lampę w salonie.", LAMPA])
    service, _, clock, store = make(engine=engine)
    turn(service, "zapamiętaj, że lampa stoi w salonie")
    before = store.concepts(["lamp"])["lamp"].strength
    r = turn(service, "Co pamiętasz o lampie?")
    assert r["text"] == "Pamiętam lampę w salonie." and engine.offered[:2] == [
        ["iris_recall", "iris_remember"], ["iris_recall", "iris_remember"]]
    tool_result = engine.calls[1][1][-1].tool_results[0]
    assert tool_result.content.startswith("lampa (siła ") and "wprost" in tool_result.content
    assert events(store, "memory.recall")[-1] == {"by": "tool", "count": 3}
    recalled = store.concepts(["lamp"])["lamp"].strength
    assert recalled > before
    assert turn(service, "dzięki")["text"] == "Dobrze, wzmacniam pojęcia użyte w tej odpowiedzi."
    assert store.concepts(["lamp"])["lamp"].strength > recalled
    # Druga pochwała pod rząd nie jest już reakcją: lista została zużyta.
    assert turn(service, "dzięki")["model"] == "scripted"


def test_criticism_weakens_and_forgets_recalled_concepts():
    engine = ToolEngine([tool_call("iris_recall", query="lampa"), "Lampa jest w kuchni.", "{}"])
    service, _, _, store = make(engine=engine, memory={"feedback_down": 0.9})
    turn(service, "zapamiętaj, że lampa")
    store.save_memory([dataclasses.replace(store.concepts(["lamp"])["lamp"], strength=0.03)])
    turn(service, "Gdzie lampa?")  # przypomnienie: 0,03 → 0,1755; krytyka: razy 0,1 → poniżej progu
    assert turn(service, "to nie tak")["text"] == "Dobrze, osłabiam pojęcia użyte w tej odpowiedzi."
    assert store.concept_count() == 0
    assert events(store, "memory.forget")[-1] == {"why": "criticized", "count": 1}


def test_remote_engine_without_memory_permission_gets_only_remember_and_no_context():
    engine = ToolEngine(["Dobrze."], remote=True)
    service, _, _, _ = make(engine=engine)
    turn(service, "zapamiętaj, że lampa stoi w salonie")
    turn(service, "Gdzie stoi lampa?")
    assert engine.offered[0] == ["iris_remember"]
    assert "Pamięć IRIS" not in engine.chat_calls[0][1][-1].content


def test_engine_without_tools_gets_recalled_concepts_in_the_context():
    service, engine, _, _ = make(["Dobrze."])
    turn(service, "zapamiętaj, że lampa stoi w salonie")
    turn(service, "Gdzie stoi lampa?")
    assert "Pamięć IRIS, pojęcia przypomniane do tej wypowiedzi:\n- lampa (siła" in engine.chat_calls[0][1][-1].content


def test_remember_tool_filters_sensitive_words():
    engine = ToolEngine([tool_call("iris_remember", text="biorę leki na serce i lubię jazz"), "Dobrze.", "{}"])
    service, _, _, store = make(engine=engine)
    turn(service, "Proszę, pamiętaj o tym")
    assert "leki" not in labels(store) and "jazz" in labels(store)
    assert events(store, "memory.learn")[0]["how"] == "tool"


# --- zanik ---


def test_prune_at_start_and_on_tick_forgets_old_concepts():
    service, _, clock, store = make(with_assistant=False)
    turn(service, "zapamiętaj, że lampa")
    clock.advance(60 * 86400)
    service.tick()
    assert store.concept_count() == 0 and store.forgotten_count() == 1
    assert events(store, "memory.forget")[-1] == {"why": "decay", "count": 1}


# --- REST ---


def rest(**config):
    clock = ManualClock()
    cfg = Config.model_validate(config)
    app = create_app(cfg, clock=clock, store=Store(clock=clock), run_tick_loop=False, token=None)
    return app


def test_rest_memory_routes():
    with local_client(rest()) as c:
        assert c.get("/memory").json()["nodes"] == []
        r = c.post("/memory/learn", json={"text": "Lampa w salonie", "domain": "dom"})
        assert r.status_code == 200 and r.json() == {"fresh": ["lampa", "salonie"], "reinforced": [], "domain": "dom"}
        snap = c.get("/memory").json()
        assert [n["label"] for n in snap["nodes"]] == ["lampa", "salonie"] and len(snap["links"]) == 1
        node = snap["nodes"][0]
        assert node["contexts"] == {"dom": 1} and node["effective"] == 0.35 and node["hits"] == 1
        assert snap["params"]["half_life_days"] == 14 and [d["id"] for d in snap["domains"]] == [
            "dom", "czas", "ty", "rozmowa"]
        hits = c.post("/memory/recall", json={"query": "lampa"}).json()["results"]
        assert [(h["label"], h["via"]) for h in hits] == [("lampa", "match"), ("salonie", "link")]
        assert c.post("/memory/learn", json={"text": "x", "extra": 1}).status_code == 422
        assert c.post("/memory/recall", json={"query": ""}).status_code == 422
        assert c.delete("/memory/concepts/lamp").status_code == 204
        assert c.delete("/memory/concepts/lamp").status_code == 404
        assert c.delete("/memory").status_code == 422
        assert c.delete("/memory?confirm=true").status_code == 204
        assert c.get("/memory").json()["nodes"] == []
        assert c.get("/facts").status_code == 404


def test_rest_learn_is_409_when_memory_is_disabled():
    with local_client(rest(memory={"enabled": False})) as c:
        assert c.post("/memory/learn", json={"text": "lampa"}).status_code == 409
        assert c.get("/memory").status_code == 200


def test_rest_memory_requires_the_token():
    clock = ManualClock()
    app = create_app(Config(), clock=clock, store=Store(clock=clock), run_tick_loop=False, token=TOKEN)
    with local_client(app) as c:
        assert c.get("/memory").status_code == 401
        assert c.get("/memory", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


# --- strumień ---


def test_stream_sends_the_graph_and_every_change():
    app = rest()
    with local_client(app) as c:
        with c.websocket_connect("/memory/stream", subprotocols=["iris"]) as ws:
            assert ws.receive_json()["nodes"] == []
            c.post("/memory/learn", json={"text": "lampa"})
            assert [n["label"] for n in ws.receive_json()["nodes"]] == ["lampa"]


def test_stream_refuses_without_the_token_and_limits_clients():
    clock = ManualClock()
    app = create_app(Config(), clock=clock, store=Store(clock=clock), run_tick_loop=False, token=TOKEN)
    with local_client(app) as c:
        with c.websocket_connect("/memory/stream", subprotocols=["iris"]) as ws:
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_json()
            assert e.value.code == 4003
    app = rest(memory={"stream_max_clients": 1})
    with local_client(app) as c:
        with c.websocket_connect("/memory/stream", subprotocols=["iris"]) as first:
            first.receive_json()
            with c.websocket_connect("/memory/stream", subprotocols=["iris"]) as second:
                with pytest.raises(WebSocketDisconnect) as e:
                    second.receive_json()
                assert e.value.code == 4001


# --- konfiguracja i migracja ---


def test_old_config_keys_stop_the_host_with_a_clear_message():
    with pytest.raises(ValueError, match=r"\[learning\] zastąpiła \[memory\]"):
        Config.model_validate({"learning": {"enabled": True}})
    with pytest.raises(ValueError, match="send_facts zastąpiło send_memory"):
        LLMConfig.model_validate({"send_facts": "always"})
    with pytest.raises(ValueError, match="max_facts"):
        LLMConfig.model_validate({"max_facts": 3})


def test_facts_from_version_3_teach_the_graph_and_stay_in_facts_v3(tmp_path):
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    from host.store.schema import _MIGRATIONS

    conn.executescript(f"BEGIN;{_MIGRATIONS[0]}PRAGMA user_version = 3;COMMIT;")
    conn.executemany(
        "INSERT INTO facts(text, source, confidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        [("Mam psa o imieniu Burek", "stated", 1.0, "2026-10-01T10:00:00.000+00:00", "2026-10-01T10:00:00.000+00:00"),
         ("Pies Burek ma 3 lata", "inferred", 0.6, "2026-10-02T10:00:00.000+00:00", "2026-10-02T10:00:00.000+00:00"),
         ("Biorę leki na ciśnienie", "stated", 1.0, "2026-10-03T10:00:00.000+00:00", "2026-10-03T10:00:00.000+00:00")])
    conn.commit()
    conn.close()
    store = Store(path)
    concepts = store.concepts()
    assert sorted(c.label for c in concepts.values()) == [
        "biorę", "burek", "ciśnienie", "imieniu", "lata", "leki", "pies", "psa"]
    assert concepts["burek"].hits == 2 and concepts["burek"].strength == pytest.approx(0.35 * 0.5 ** (1 / 14) * 0.6 + 0.4)
    assert store.has_facts_v3() and conn_version(path) == 4
    assert ("burek", "pies") in store.links_between(["pies", "burek"])
    store.close()


def conn_version(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def test_failed_fact_migration_leaves_version_3(tmp_path, monkeypatch):
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    from host.store import store as store_module
    from host.store.schema import _MIGRATIONS

    conn.executescript(f"BEGIN;{_MIGRATIONS[0]}PRAGMA user_version = 3;COMMIT;")
    conn.execute("INSERT INTO facts(text, source, confidence, created_at, updated_at) VALUES "
                 "('lampa', 'stated', 1, '2026-10-01T10:00:00.000+00:00', '2026-10-01T10:00:00.000+00:00')")
    conn.commit()
    conn.close()

    def broken(cfg):
        def run(c):
            raise RuntimeError("awaria w trakcie migracji")
        return run

    monkeypatch.setattr(store_module, "_migrate_facts", broken)
    with pytest.raises(RuntimeError):
        Store(path)
    assert conn_version(path) == 3
    c = sqlite3.connect(path)
    assert c.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1  # tabela nadal ma starą nazwę
    c.close()


def test_stream_ignores_binary_frames_from_the_client():
    app = rest()
    with local_client(app) as c:
        with c.websocket_connect("/memory/stream", subprotocols=["iris"]) as ws:
            ws.receive_json()
            ws.send_bytes(b"\x00\x01")
            ws.send_text("cokolwiek")
            c.post("/memory/learn", json={"text": "lampa"})
            assert [n["label"] for n in ws.receive_json()["nodes"]] == ["lampa"]


def test_failed_database_write_records_no_learning(monkeypatch, caplog):
    service, _, _, store = make(with_assistant=False)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "save_memory", locked)
    with caplog.at_level("ERROR"):
        assert turn(service, "zapamiętaj, że lampa")["text"] == "Nie udało się zapisać w pamięci. Spróbuj ponownie."
    assert events(store, "memory.learn") == []
