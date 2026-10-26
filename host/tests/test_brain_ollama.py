import asyncio
import json
import logging
import socket
import threading

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from host.brain.llm import (
    LLMConfigError,
    LLMError,
    LLMUnavailable,
    Message,
    with_retries,
)
from host.brain.ollama import OllamaEngine

OK = {
    "model": "bielik:11b",
    "message": {"role": "assistant", "content": "Jest 14:05."},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 42,
    "eval_count": 7,
}


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def engine_for(handler, **kw):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    eng = OllamaEngine("bielik:11b", client=client, sleep=fake_sleep, **kw)
    return eng, sleeps


def ask(eng, text="Która godzina?"):
    return asyncio.run(eng.complete("SYSTEM", [Message("user", text)], max_tokens=50))


def json_response(data, status=200):
    return httpx.Response(status, json=data)


# --- zapytanie i odpowiedź ---


def test_request_shape():
    rec = Recorder(json_response(OK))
    eng, _ = engine_for(rec)
    ask(eng)
    (req,) = rec.requests
    assert str(req.url) == "http://localhost:11434/api/chat"
    body = json.loads(req.content)
    assert body == {
        "model": "bielik:11b",
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "Która godzina?"},
        ],
        "stream": False,
        "options": {"num_predict": 50},
    }


def test_temperature_only_when_configured_and_roles_kept():
    rec = Recorder(json_response(OK))
    eng, _ = engine_for(rec, temperature=0.2)
    asyncio.run(eng.complete("S", [Message("user", "a"), Message("assistant", "b"), Message("user", "c")], max_tokens=9))
    body = json.loads(rec.requests[0].content)
    assert body["options"] == {"num_predict": 9, "temperature": 0.2}
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user"]


def test_base_url_is_normalized():
    rec = Recorder(json_response(OK))
    eng, _ = engine_for(rec, base_url="http://gpu-box:11434/")
    ask(eng)
    assert str(rec.requests[0].url) == "http://gpu-box:11434/api/chat"


def test_parses_reply_and_usage():
    eng, _ = engine_for(Recorder(json_response(OK)))
    reply = ask(eng)
    assert (reply.text, reply.finish, reply.model) == ("Jest 14:05.", "stop", "bielik:11b")
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (42, 7)


@pytest.mark.parametrize(
    "extra, finish",
    [({"done_reason": "length"}, "length"), ({"done_reason": "load"}, "other"), ({"done_reason": None}, "stop")],
)
def test_finish_reason_mapping(extra, finish):
    eng, _ = engine_for(Recorder(json_response({**OK, **extra})))
    assert ask(eng).finish == finish


def test_missing_counts_are_none():
    data = {k: v for k, v in OK.items() if k not in ("prompt_eval_count", "eval_count")}
    eng, _ = engine_for(Recorder(json_response(data)))
    u = ask(eng).usage
    assert (u.input_tokens, u.output_tokens) == (None, None)


# --- błędy ---


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="to nie json"),
        json_response({"message": {"role": "assistant"}, "done": True}),
        json_response({"message": {"content": 5}, "done": True}),
        json_response([1, 2]),
        json_response({**OK, "done": False}),
    ],
)
def test_malformed_response_is_llm_error_without_retry(response):
    rec = Recorder(response)
    eng, sleeps = engine_for(rec, retries=2)
    with pytest.raises(LLMError) as e:
        ask(eng)
    assert not isinstance(e.value, (LLMUnavailable, LLMConfigError))
    assert len(rec.requests) == 1 and sleeps == []


def test_404_is_config_error_without_retry():
    rec = Recorder(json_response({"error": 'model "bielik:11b" not found, try pulling it first'}, 404))
    eng, sleeps = engine_for(rec, retries=3)
    with pytest.raises(LLMConfigError, match="not found"):
        ask(eng)
    assert len(rec.requests) == 1 and sleeps == []


def test_other_4xx_is_not_retried():
    rec = Recorder(json_response({"error": "zły format"}, 400))
    eng, _ = engine_for(rec, retries=3)
    with pytest.raises(LLMError, match="400"):
        ask(eng)
    assert len(rec.requests) == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_status_then_success(status):
    rec = Recorder(json_response({"error": "zajęty"}, status), json_response(OK))
    eng, sleeps = engine_for(rec, retries=1)
    assert ask(eng).text == "Jest 14:05."
    assert len(rec.requests) == 2 and sleeps == [0.5]


def test_retries_exhausted_raise_unavailable_with_backoff():
    rec = Recorder(json_response({"error": "padł"}, 500))
    eng, sleeps = engine_for(rec, retries=2)
    with pytest.raises(LLMUnavailable, match="500"):
        ask(eng)
    assert len(rec.requests) == 3 and sleeps == [0.5, 1.0]


def test_zero_retries_means_single_attempt():
    rec = Recorder(json_response({"error": "padł"}, 500))
    eng, sleeps = engine_for(rec, retries=0)
    with pytest.raises(LLMUnavailable):
        ask(eng)
    assert len(rec.requests) == 1 and sleeps == []


def test_connection_error_and_timeout_are_unavailable():
    for exc, fragment in [
        (httpx.ConnectError("odmowa"), "brak połączenia"),
        (httpx.ReadTimeout("wolno"), "limit czasu"),
        (httpx.RemoteProtocolError("urwane"), "brak połączenia"),
    ]:
        eng, _ = engine_for(Recorder(exc), retries=0)
        with pytest.raises(LLMUnavailable, match=fragment):
            ask(eng)


def test_error_body_is_truncated():
    rec = Recorder(json_response({"error": "x" * 5000}, 400))
    eng, _ = engine_for(rec, retries=0)
    with pytest.raises(LLMError) as e:
        ask(eng)
    assert len(str(e.value)) < 300


# --- lokalność ---


@pytest.mark.parametrize(
    "url, remote",
    [
        ("http://localhost:11434", False),
        ("http://127.0.0.1:11434", False),
        ("http://[::1]:11434", False),
        ("http://192.168.1.20:11434", True),
        ("https://ollama.example.com", True),
    ],
)
def test_remote_follows_host(url, remote):
    assert OllamaEngine("m", base_url=url).remote is remote


# --- prywatność logów ---


def test_message_content_never_reaches_logs(caplog):
    eng, _ = engine_for(Recorder(json_response({**OK, "message": {"role": "assistant", "content": "ODP-TAJNA"}})))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(eng.complete("SYS-TAJNY", [Message("user", "PYTANIE-TAJNE")], max_tokens=5))
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("PYTANIE-TAJNE", "ODP-TAJNA", "SYS-TAJNY"):
        assert secret not in text


# --- with_retries ---


def test_with_retries_only_retries_unavailable():
    calls = []

    async def boom():
        calls.append(1)
        raise LLMError("trwały błąd")

    with pytest.raises(LLMError):
        asyncio.run(with_retries(boom, 3))
    assert len(calls) == 1


# --- prawdziwe gniazdo ---


@pytest.fixture
def stub_server():
    app = FastAPI()
    seen = []

    @app.post("/api/chat")
    async def chat(request: Request):
        body = await request.json()
        seen.append(body)
        if body["messages"][-1]["content"] == "wolno":
            await asyncio.sleep(1.0)
        return JSONResponse(OK)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        threading.Event().wait(0.05)
    assert server.started
    yield port, seen
    server.should_exit = True
    thread.join(5)


def test_real_http_round_trip_and_timeout(stub_server):
    port, seen = stub_server

    async def scenario():
        eng = OllamaEngine("bielik:11b", base_url=f"http://127.0.0.1:{port}", timeout_s=0.3, retries=0)
        try:
            reply = await eng.complete("S", [Message("user", "cześć")], max_tokens=5)
            assert reply.text == "Jest 14:05."
            with pytest.raises(LLMUnavailable, match="limit czasu"):
                await eng.complete("S", [Message("user", "wolno")], max_tokens=5)
        finally:
            await eng.aclose()

    asyncio.run(scenario())
    assert seen[0]["stream"] is False


def test_real_connection_refused_is_unavailable():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    async def scenario():
        eng = OllamaEngine("m", base_url=f"http://127.0.0.1:{port}", retries=0)
        try:
            with pytest.raises(LLMUnavailable, match="brak połączenia"):
                await eng.complete("S", [Message("user", "a")], max_tokens=5)
        finally:
            await eng.aclose()

    asyncio.run(scenario())
