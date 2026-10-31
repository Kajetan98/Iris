import asyncio
import inspect
import logging
import re

import pytest

import anthropic
from host.brain import Assistant, LLMConfigError, LLMError, LLMRefusal, LLMUnavailable, Message
from host.brain.claude import FALLBACK_BETA, FALLBACK_MODELS, ClaudeEngine
from host.core.config import LLMConfig
from host.tests.servers import (
    anthropic_error,
    anthropic_message,
    anthropic_stub_app,
    free_port,
    start,
    stop,
)

KEY = "sk-ant-test-TAJNY-KLUCZ-123"


@pytest.fixture
def stub():
    app, state = anthropic_stub_app()
    port = free_port()
    server = start(app, port)
    state["url"] = f"http://127.0.0.1:{port}"
    yield state
    stop(*server)


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_LOG", raising=False)


def engine(stub, **kw):
    kw.setdefault("retries", 0)
    return ClaudeEngine(base_url=stub["url"], **kw)


def ask(eng, system="SYSTEM", messages=None, max_tokens=300):
    async def go():
        try:
            return await eng.complete(system, messages or [Message("user", "Która godzina?")], max_tokens=max_tokens)
        finally:
            await eng.aclose()

    return asyncio.run(go())


# --- żądanie ---


def test_default_request_uses_beta_endpoint_with_server_side_fallback(stub):
    ask(engine(stub))
    (req,) = stub["requests"]
    assert req["path"] == "/v1/messages"
    assert req["headers"]["authorization"] == f"Bearer {KEY}"  # SDK 2.x wysyła klucz jako Bearer
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", req["headers"]["anthropic-version"])
    assert FALLBACK_BETA in req["headers"]["anthropic-beta"]
    assert req["body"] == {
        "model": "claude-opus-5-5",
        "max_tokens": 2300,  # 300 odpowiedzi + 2000 zapasu na myślenie
        "system": "SYSTEM",
        "messages": [{"role": "user", "content": "Która godzina?"}],
        "output_config": {"effort": "low"},
        "fallbacks": "default",
    }


def test_no_sampling_parameters_and_no_thinking_parameter(stub):
    ask(engine(stub))
    body = stub["requests"][0]["body"]
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "stream"):
        assert forbidden not in body


def test_model_without_server_side_fallback_uses_plain_endpoint(stub):
    ask(engine(stub, model="claude-haiku-5-5"))
    req = stub["requests"][0]
    assert "anthropic-beta" not in req["headers"]
    assert "fallbacks" not in req["body"] and req["body"]["model"] == "claude-haiku-5-5"


def test_fallbacks_can_be_switched_off(stub):
    ask(engine(stub, fallbacks=False))
    req = stub["requests"][0]
    assert "fallbacks" not in req["body"] and "anthropic-beta" not in req["headers"]


@pytest.mark.parametrize("model", sorted(FALLBACK_MODELS))
def test_fallback_models_list(stub, model):
    ask(engine(stub, model=model))
    assert stub["requests"][-1]["body"]["fallbacks"] == "default"


def test_effort_off_omits_the_parameter_and_other_levels_are_sent(stub):
    ask(engine(stub, effort="off"))
    assert "output_config" not in stub["requests"][0]["body"]
    ask(engine(stub, effort="high"))
    assert stub["requests"][1]["body"]["output_config"] == {"effort": "high"}


def test_headroom_is_configurable(stub):
    ask(engine(stub, thinking_headroom_tokens=0), max_tokens=500)
    assert stub["requests"][0]["body"]["max_tokens"] == 500


def test_conversation_roles_are_kept(stub):
    msgs = [Message("user", "a"), Message("assistant", "b"), Message("user", "c")]
    ask(engine(stub), messages=msgs)
    assert [(m["role"], m["content"]) for m in stub["requests"][0]["body"]["messages"]] == [
        ("user", "a"), ("assistant", "b"), ("user", "c"),
    ]


# --- odpowiedź ---


def test_parses_text_usage_and_model(stub):
    stub["script"] = [(200, anthropic_message("Jest 14:05.", usage={"input_tokens": 100, "output_tokens": 7,
                                                                      "cache_read_input_tokens": 20,
                                                                      "cache_creation_input_tokens": 5}), {})]
    reply = ask(engine(stub))
    assert (reply.text, reply.finish, reply.model) == ("Jest 14:05.", "stop", "claude-opus-5-5")
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (125, 7)  # wszystkie rodzaje wejścia


def test_thinking_blocks_are_ignored_and_text_blocks_joined(stub):
    content = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "Pierwsza część. "},
        {"type": "text", "text": "Druga część."},
    ]
    stub["script"] = [(200, anthropic_message(content=content), {})]
    assert ask(engine(stub)).text == "Pierwsza część. Druga część."


@pytest.mark.parametrize(
    "stop_reason, finish",
    [("end_turn", "stop"), ("stop_sequence", "stop"), ("max_tokens", "length"), ("refusal", "refusal"),
     ("pause_turn", "other"), ("tool_use", "other")],
)
def test_stop_reason_mapping(stub, stop_reason, finish):
    stub["script"] = [(200, anthropic_message(stop_reason=stop_reason), {})]
    assert ask(engine(stub)).finish == finish


def test_served_by_fallback_model_is_reported(stub, caplog):
    stub["script"] = [(200, anthropic_message(model="claude-opus-4-8"), {})]
    with caplog.at_level(logging.INFO):
        reply = ask(engine(stub))
    assert reply.model == "claude-opus-4-8"
    assert any("modelu zapasowego" in r.getMessage() for r in caplog.records)


def test_refusal_becomes_llm_refusal_in_the_assistant(stub):
    stub["script"] = [(200, anthropic_message("", stop_reason="refusal", content=[],
                                              stop_details={"type": "refusal", "category": "cyber", "explanation": None}), {})]
    cfg = LLMConfig(engine="claude")
    assistant = Assistant(engine(stub), cfg)
    with pytest.raises(LLMRefusal):
        asyncio.run(assistant.ask("Cześć", state={"mode": "nominal", "drives": []}))
    assert assistant.history == []


def test_truncated_answer_is_flagged_by_the_assistant(stub):
    stub["script"] = [(200, anthropic_message("Początek odpowiedzi", stop_reason="max_tokens"), {})]
    answer = asyncio.run(Assistant(engine(stub), LLMConfig(engine="claude")).ask("Cześć", state={"mode": "nominal", "drives": []}))
    assert answer.truncated and answer.text == "Początek odpowiedzi"


# --- błędy ---


@pytest.mark.parametrize(
    "status, error_type, expected",
    [
        (401, "authentication_error", LLMConfigError),
        (403, "permission_error", LLMConfigError),
        (404, "not_found_error", LLMConfigError),
        (400, "invalid_request_error", LLMError),
        (413, "request_too_large", LLMError),
        (429, "rate_limit_error", LLMUnavailable),
        (500, "api_error", LLMUnavailable),
        (529, "overloaded_error", LLMUnavailable),
    ],
)
def test_http_errors_are_mapped(stub, status, error_type, expected):
    stub["script"] = [(status, anthropic_error(error_type, "opis błędu"), {})]
    with pytest.raises(expected) as e:
        ask(engine(stub))
    assert type(e.value) is expected  # dokładny typ: konfiguracja i niedostępność się nie mieszają
    assert str(status) in str(e.value) and "opis błędu" in str(e.value)
    assert len(stub["requests"]) == 1  # retries=0


def test_transient_errors_are_retried_by_the_sdk(stub):
    stub["script"] = [(529, anthropic_error("overloaded_error"), {}), (500, anthropic_error("api_error"), {}),
                      (200, anthropic_message("W końcu."), {})]
    assert ask(engine(stub, retries=2)).text == "W końcu."
    assert len(stub["requests"]) == 3


def test_retries_exhausted_raise_unavailable(stub):
    stub["script"] = [(529, anthropic_error("overloaded_error"), {})]
    with pytest.raises(LLMUnavailable):
        ask(engine(stub, retries=1))
    assert len(stub["requests"]) == 2


def test_client_errors_are_not_retried(stub):
    stub["script"] = [(400, anthropic_error("invalid_request_error", "zły parametr"), {})]
    with pytest.raises(LLMError):
        ask(engine(stub, retries=3))
    assert len(stub["requests"]) == 1


def test_timeout_is_unavailable(stub):
    stub["delay"] = 1.0
    with pytest.raises(LLMUnavailable, match="limit czasu"):
        ask(engine(stub, timeout_s=0.3))


def test_connection_refused_is_unavailable():
    eng = ClaudeEngine(base_url=f"http://127.0.0.1:{free_port()}", retries=0)
    with pytest.raises(LLMUnavailable, match="brak połączenia"):
        ask(eng)


def test_missing_credentials_are_a_config_error(stub, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY"):
        ask(engine(stub))
    assert stub["requests"] == []  # żądanie nie wyszło


# --- zdalność ---


@pytest.mark.parametrize(
    "url, remote",
    [(None, True), ("https://api.anthropic.com", True), ("http://gateway.lan:8080", True),
     ("http://localhost:8080", False), ("http://127.0.0.1:8080", False)],
)
def test_remote_flag(url, remote):
    assert ClaudeEngine(base_url=url).remote is remote


# --- prywatność ---


def test_key_and_conversation_never_reach_logs(stub, caplog):
    stub["script"] = [(200, anthropic_message("ODPOWIEDZ-TAJNA-31"), {})]
    with caplog.at_level(logging.DEBUG):
        ask(engine(stub), system="SYS-TAJNY-31", messages=[Message("user", "PYTANIE-TAJNE-31")])
    text = caplog.text
    for secret in (KEY, "TAJNY-KLUCZ", "SYS-TAJNY-31", "PYTANIE-TAJNE-31", "ODPOWIEDZ-TAJNA-31"):
        assert secret not in text


def test_errors_do_not_leak_the_key(stub):
    stub["script"] = [(401, anthropic_error("authentication_error", "invalid x-api-key"), {})]
    with pytest.raises(LLMConfigError) as e:
        ask(engine(stub))
    assert KEY not in str(e.value)


# --- zgodność z prawdziwym SDK ---


def test_request_arguments_exist_in_the_installed_sdk():
    plain = inspect.signature(anthropic.AsyncAnthropic().messages.create).parameters
    beta = inspect.signature(anthropic.AsyncAnthropic().beta.messages.create).parameters
    for name in ("model", "max_tokens", "system", "messages", "output_config"):
        assert name in plain and name in beta
    assert "betas" in beta and "fallbacks" in beta


def test_client_options_exist_in_the_installed_sdk():
    params = inspect.signature(anthropic.AsyncAnthropic.__init__).parameters
    for name in ("base_url", "timeout", "max_retries"):
        assert name in params


# --- polityka faktów i cała ścieżka ---

from host.api.main import create_app  # noqa: E402
from host.brain import build_assistant, build_engine  # noqa: E402
from host.core.clock import ManualClock  # noqa: E402
from host.core.config import Config  # noqa: E402
from host.store import Store  # noqa: E402
from host.tests.brain_helpers import fact  # noqa: E402
from host.tests.helpers import local_client  # noqa: E402

STATE = {"mode": "nominal", "drives": []}


def sent_text(stub):
    body = stub["requests"][-1]["body"]
    return body["system"] + "\n" + "\n".join(m["content"] for m in body["messages"])


class RemoteClaude(ClaudeEngine):
    """Silnik skierowany na atrapę (adres lokalny), ale zachowujący się jak prawdziwa chmura."""

    remote = True


def test_facts_stay_home_by_default_with_claude(stub):
    facts = [fact(1, "Mieszkam w Gdańsku")]
    eng = RemoteClaude(base_url=stub["url"], retries=0)
    asyncio.run(Assistant(eng, LLMConfig(engine="claude")).ask("Cześć", state=STATE, facts=facts))
    assert "Gdańsku" not in sent_text(stub)  # send_facts=local_only, a chmura jest zdalna


def test_facts_reach_a_loopback_gateway_by_default(stub):
    facts = [fact(1, "Mieszkam w Gdańsku")]
    assert engine(stub).remote is False  # adres pętli zwrotnej liczy się jako lokalny (spec, sekcja 12)
    asyncio.run(Assistant(engine(stub), LLMConfig(engine="claude")).ask("Cześć", state=STATE, facts=facts))
    assert "Mieszkam w Gdańsku" in sent_text(stub)


def test_facts_are_sent_when_explicitly_allowed(stub):
    facts = [fact(1, "Mieszkam w Gdańsku")]
    cfg = LLMConfig(engine="claude", send_facts="always")
    asyncio.run(Assistant(RemoteClaude(base_url=stub["url"], retries=0), cfg).ask("Cześć", state=STATE, facts=facts))
    assert "Mieszkam w Gdańsku" in sent_text(stub)


def test_sensor_payloads_never_reach_claude(stub):
    from host.core.core import HostCore
    from host.tests.helpers import heartbeat, make_event

    core = HostCore(clock=ManualClock())
    core.handle_event(heartbeat(18))
    core.handle_event(make_event("body.sensor", {"gps": {"lat": 52.2297, "lon": 21.0122}, "imu": [7, 7, 7], "presence": True}))
    eng = RemoteClaude(base_url=stub["url"], retries=0)
    asyncio.run(Assistant(eng, LLMConfig(engine="claude")).ask("Gdzie jestem?", state=core.prompt_slice()))
    sent = sent_text(stub)
    assert "52.2297" not in sent and "21.0122" not in sent and "[7, 7, 7]" not in sent
    assert "bateria: brak danych → 18%" in sent  # sam stan trafia


def test_factory_builds_claude_engine_and_assistant():
    cfg = LLMConfig(engine="claude", effort="high", fallbacks=False, thinking_headroom_tokens=500, base_url="http://localhost:9")
    eng = build_engine(cfg)
    assert isinstance(eng, ClaudeEngine) and eng.model == "claude-opus-5-5" and not eng.remote
    assert (eng._effort, eng._use_fallbacks, eng._headroom) == ("high", False, 500)
    assert isinstance(build_assistant(LLMConfig(engine="claude")), Assistant)


def test_post_chat_through_a_config_built_claude_engine(stub):
    stub["script"] = [(200, anthropic_message("**Jest** południe."), {})]
    cfg = Config.model_validate(
        {"store": {"path": ":memory:"}, "brain": {"llm": {"engine": "claude", "base_url": stub["url"], "retries": 0}}}
    )
    app = create_app(cfg, ManualClock(), run_tick_loop=False)
    with local_client(app) as c:
        r = c.post("/chat", json={"text": "Która godzina?"})
        assert r.status_code == 200 and r.json()["text"] == "Jest południe."
        assert r.json()["model"] == "claude-opus-5-5" and r.json()["usage"] == {"input_tokens": 120, "output_tokens": 9}
        stub["script"] = [(529, anthropic_error("overloaded_error"), {})]
        assert c.post("/chat", json={"text": "Jeszcze raz"}).status_code == 503
        stub["script"] = [(200, anthropic_message("", stop_reason="refusal", content=[]), {})]
        assert c.post("/chat", json={"text": "Trzeci raz"}).status_code == 422
    assert stub["requests"][0]["body"]["system"].startswith("Jesteś IRIS")


def test_missing_key_gives_a_clear_http_error(stub, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    cfg = Config.model_validate(
        {"store": {"path": ":memory:"}, "brain": {"llm": {"engine": "claude", "base_url": stub["url"]}}}
    )
    with local_client(create_app(cfg, ManualClock(), run_tick_loop=False)) as c:
        r = c.post("/chat", json={"text": "Cześć"})
    assert r.status_code == 502 and "ANTHROPIC_API_KEY" in r.json()["detail"]
