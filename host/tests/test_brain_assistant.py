import asyncio
import json

import pytest

from host.brain import (
    Assistant,
    LLMConfigError,
    LLMError,
    LLMRefusal,
    LLMReply,
    LLMUnavailable,
    Usage,
    build_assistant,
    build_engine,
)
from host.brain.ollama import OllamaEngine
from host.brain.prompt import CONTEXT_HEADER, SYSTEM_PROMPT, USER_HEADER
from host.core.clock import ManualClock
from host.core.config import Config, LLMConfig
from host.core.core import HostCore
from host.tests.brain_helpers import T0, ScriptedEngine, fact
from host.tests.helpers import heartbeat, make_event

STATE = {"mode": "nominal", "drives": []}


def cfg(**kw):
    return LLMConfig.model_validate({"engine": "ollama", "model": "m", **kw})


def run(coro):
    return asyncio.run(coro)


def make(replies=("Dobrze.",), *, remote=False, **kw):
    engine = ScriptedEngine(replies, remote=remote)
    return Assistant(engine, cfg(**kw)), engine


# --- zapytanie do silnika ---


def test_engine_gets_system_prompt_context_and_max_tokens():
    a, engine = make(max_tokens=120)
    run(a.ask("Która godzina?", state=STATE, now=T0))
    system, messages, max_tokens = engine.calls[0]
    assert system == SYSTEM_PROMPT and max_tokens == 120
    assert len(messages) == 1 and messages[0].role == "user"
    assert messages[0].content.startswith(CONTEXT_HEADER)
    assert messages[0].content.endswith(f"{USER_HEADER}\nKtóra godzina?")


def test_answer_fields_and_speech_cleanup():
    reply = LLMReply("**Jest** 14:05.\n", "stop", "bielik", Usage(30, 4))
    a, _ = make([reply])
    ans = run(a.ask("  Która godzina?  ", state=STATE))
    assert (ans.text, ans.truncated, ans.model) == ("Jest 14:05.", False, "bielik")
    assert (ans.usage.input_tokens, ans.usage.output_tokens) == (30, 4)


def test_blank_utterance_is_rejected_before_calling_the_engine():
    a, engine = make()
    with pytest.raises(ValueError):
        run(a.ask("   ", state=STATE))
    assert engine.calls == []


# --- historia ---


def test_history_keeps_plain_turns_and_context_only_in_current_turn():
    a, engine = make(["Pierwsza.", "Druga."])
    run(a.ask("Pytanie jeden", state={"mode": "saving", "drives": ["battery_low"]}, now=T0))
    run(a.ask("Pytanie dwa", state=STATE, now=T0))
    _, second_call, _ = engine.calls[1]
    assert [(m.role, m.content[:20]) for m in second_call[:2]] == [
        ("user", "Pytanie jeden"), ("assistant", "Pierwsza."),
    ]
    assert CONTEXT_HEADER not in second_call[0].content  # stary kontekst nie wraca
    assert second_call[2].content.startswith(CONTEXT_HEADER)
    assert [m.content for m in a.history] == ["Pytanie jeden", "Pierwsza.", "Pytanie dwa", "Druga."]


def test_history_is_trimmed_to_configured_turns():
    a, engine = make(["a1", "a2", "a3"], history_turns=2)
    for q in ("q1", "q2", "q3"):
        run(a.ask(q, state=STATE))
    assert [m.content for m in a.history] == ["q2", "a2", "q3", "a3"]
    assert len(engine.calls[2][1]) == 5  # 2 pary z historii + bieżąca tura


def test_zero_history_turns_forgets_everything():
    a, engine = make(["a1", "a2"], history_turns=0)
    run(a.ask("q1", state=STATE))
    run(a.ask("q2", state=STATE))
    assert a.history == [] and len(engine.calls[1][1]) == 1


def test_reset_clears_history():
    a, _ = make()
    run(a.ask("q", state=STATE))
    a.reset()
    assert a.history == []


# --- błędy ---


@pytest.mark.parametrize("error", [LLMUnavailable("brak"), LLMConfigError("model"), LLMError("zły format")])
def test_engine_error_propagates_and_leaves_history_unchanged(error):
    a, _ = make(["ok", error])
    run(a.ask("q1", state=STATE))
    before = a.history
    with pytest.raises(type(error)):
        run(a.ask("q2", state=STATE))
    assert a.history == before


def test_refusal_is_reported_and_not_remembered():
    a, _ = make([LLMReply("", "refusal", "m")])
    with pytest.raises(LLMRefusal):
        run(a.ask("q", state=STATE))
    assert a.history == []


def test_empty_reply_is_an_error():
    a, _ = make(["  \n "])
    with pytest.raises(LLMError, match="pust"):
        run(a.ask("q", state=STATE))
    assert a.history == []


def test_truncated_reply_is_flagged_but_returned():
    a, _ = make([LLMReply("Początek odpowiedzi", "length", "m")])
    ans = run(a.ask("q", state=STATE))
    assert ans.truncated and ans.text == "Początek odpowiedzi"


def test_concurrent_asks_are_serialized():
    async def scenario():
        order = []

        class Slow(ScriptedEngine):
            async def complete(self, system, messages, *, max_tokens):
                order.append(("start", messages[-1].content.splitlines()[-1]))
                await asyncio.sleep(0.05)
                order.append(("end", messages[-1].content.splitlines()[-1]))
                return await super().complete(system, messages, max_tokens=max_tokens)

        a = Assistant(Slow(["r1", "r2"]), cfg())
        await asyncio.gather(a.ask("pierwsze", state=STATE), a.ask("drugie", state=STATE))
        assert [k for k, _ in order] == ["start", "end", "start", "end"]
        assert len(a.history) == 4

    run(scenario())


def test_aclose_closes_engine():
    a, engine = make()
    run(a.aclose())
    assert engine.closed


# --- polityka faktów w praktyce ---


FACTS = [fact(1, "Mieszkam w Gdańsku"), fact(2, "Lubi kawę", "inferred", 0.8)]


def facts_in_call(engine):
    return "Mieszkam w Gdańsku" in engine.calls[0][1][-1].content


def test_local_engine_gets_facts_by_default():
    a, engine = make(remote=False)
    run(a.ask("q", state=STATE, facts=FACTS))
    assert facts_in_call(engine)
    assert "(przypuszczenie, pewność 0.8)" in engine.calls[0][1][-1].content


def test_remote_engine_gets_no_facts_by_default():
    a, engine = make(remote=True)
    run(a.ask("q", state=STATE, facts=FACTS))
    assert not facts_in_call(engine)
    assert "Znane fakty" not in engine.calls[0][1][-1].content


def test_remote_engine_gets_facts_when_allowed():
    a, engine = make(remote=True, send_facts="always")
    run(a.ask("q", state=STATE, facts=FACTS))
    assert facts_in_call(engine)


def test_never_policy_withholds_facts_even_locally():
    a, engine = make(remote=False, send_facts="never")
    run(a.ask("q", state=STATE, facts=FACTS))
    assert not facts_in_call(engine)


# --- z HostCore ---


def test_prompt_slice_from_core_reaches_engine_without_sensor_payloads():
    clock = ManualClock()
    core = HostCore(clock=clock)
    core.handle_event(heartbeat(18))
    core.handle_event(make_event("body.sensor", {"gps": {"lat": 52.2297, "lon": 21.0122}, "imu": [9, 9, 9], "presence": True}))
    for _ in range(11):
        clock.advance(1)
        core.handle_event(heartbeat(18))
    a, engine = make(remote=True)
    run(a.ask("Jak z baterią?", state=core.prompt_slice(), now=T0))
    sent = json.dumps([m.content for m in engine.calls[0][1]], ensure_ascii=False)
    assert "oszczędzanie energii" in sent and "niski poziom baterii ciała" in sent
    assert "bateria: brak danych → 18%" in sent
    for leaked in ("52.2297", "21.0122", "[9, 9, 9]"):
        assert leaked not in sent


# --- konfiguracja i fabryka ---


def test_engine_none_builds_nothing():
    assert build_engine(LLMConfig()) is None
    assert build_assistant(LLMConfig()) is None
    assert Config().brain.llm.engine == "none"


def test_ollama_engine_from_config():
    conf = cfg(base_url="http://192.168.1.5:11434", timeout_s=7, retries=3, temperature=0.4)
    engine = build_engine(conf)
    assert isinstance(engine, OllamaEngine) and engine.model == "m" and engine.remote
    assert isinstance(build_assistant(conf), Assistant)


def test_model_is_required_when_engine_is_set():
    with pytest.raises(Exception, match="model"):
        LLMConfig(engine="ollama")


def test_toml_section(tmp_path):
    p = tmp_path / "iris.toml"
    p.write_text('[brain.llm]\nengine = "ollama"\nmodel = "bielik:11b"\nsend_facts = "never"\nmax_tokens = 150\n')
    c = Config.load(p).brain.llm
    assert (c.engine, c.model, c.send_facts, c.max_tokens, c.history_turns) == ("ollama", "bielik:11b", "never", 150, 10)


@pytest.mark.parametrize("bad", [{"engine": "gpt"}, {"send_facts": "sometimes"}, {"retries": 9}, {"max_tokens": 0}, {"temperature": 3}])
def test_invalid_llm_config_is_rejected(bad):
    with pytest.raises(Exception):
        LLMConfig.model_validate({"engine": "ollama", "model": "m", **bad})
