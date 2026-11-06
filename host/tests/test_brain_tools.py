"""Narzędzia pamięci w rozmowie (docs/spec.md, sekcja 12, "Narzędzia pamięci")."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from host.brain import Assistant, LLMReply, Message
from host.brain.claude import ClaudeEngine
from host.brain.llm import LLMToolsUnsupported, ToolCall, ToolResult, ToolSpec
from host.brain.ollama import OllamaEngine
from host.brain.tools import RECALL, REMEMBER
from host.core.config import LLMConfig
from host.tests.servers import anthropic_message, anthropic_stub_app, free_port, start, stop

STATE = {"mode": "nominal", "drives": []}


class FakeTools:
    """Narzędzia tury: zapisuje wywołania, `iris_recall` zwraca stały tekst."""

    def __init__(self, specs=(RECALL, REMEMBER), context=("lampa (siła 0,40, wprost)",), boom=False):
        self._specs = list(specs)
        self._context = list(context)
        self.boom = boom
        self.calls: list[ToolCall] = []
        self.context_asked = 0

    def specs(self, *, remote):
        return self._specs

    async def call(self, call):
        self.calls.append(call)
        if self.boom:
            raise RuntimeError("baza padła")
        return ToolResult(call.id, call.name, "lampa (siła 0,62, wprost)")

    async def context(self, text, *, remote):
        self.context_asked += 1
        return self._context


def run(coro):
    return asyncio.run(coro)


# ---- Claude ---------------------------------------------------------------------------------------------

THINKING = {"type": "thinking", "thinking": "", "signature": "sig-123"}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "iris_recall", "input": {"query": "lampa"}}


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    app, state = anthropic_stub_app()
    port = free_port()
    server = start(app, port)
    state["url"] = f"http://127.0.0.1:{port}"
    yield state
    stop(*server)


def claude_assistant(stub, **cfg):
    engine = ClaudeEngine(base_url=stub["url"], retries=0)
    return Assistant(engine, LLMConfig(engine="claude", **cfg))


def test_claude_tool_round_echoes_the_reply_unchanged_and_keeps_only_text_in_history(stub):
    stub["script"] = [
        (200, anthropic_message(content=[THINKING, TOOL_USE], stop_reason="tool_use"), {}),
        (200, anthropic_message("Pamiętam lampę w salonie."), {}),
    ]
    assistant = claude_assistant(stub)
    tools = FakeTools()

    async def go():
        try:
            return await assistant.ask("Co pamiętasz o lampie?", state=STATE, tools=tools)
        finally:
            await assistant.aclose()

    answer = run(go())
    assert answer.text == "Pamiętam lampę w salonie." and answer.tool_rounds == 1
    assert tools.calls == [ToolCall("toolu_1", "iris_recall", {"query": "lampa"})]
    first, second = (r["body"] for r in stub["requests"])
    assert [t["name"] for t in first["tools"]] == ["iris_recall", "iris_remember"]
    assert first["tools"][0]["input_schema"]["required"] == ["query"]
    assert "tool_choice" not in first
    assistant_turn, results = second["messages"][1], second["messages"][2]
    assert assistant_turn["role"] == "assistant"
    assert assistant_turn["content"][0] == THINKING  # blok myślenia z podpisem wraca bez zmian
    assert assistant_turn["content"][1]["id"] == "toolu_1"
    assert results == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "lampa (siła 0,62, wprost)", "is_error": False}]}
    assert [m.role for m in assistant.history] == ["user", "assistant"]
    assert assistant.history[0].content == "Co pamiętasz o lampie?" and not assistant.history[1].tool_calls
    # Model dostał iris_recall, więc pojęcia nie trafiły do bloku kontekstu.
    assert tools.context_asked == 0 and "Pamięć IRIS" not in first["messages"][0]["content"]


def test_after_the_last_round_the_model_gets_no_tools(stub):
    stub["script"] = [(200, anthropic_message(content=[TOOL_USE], stop_reason="tool_use"), {})] * 2 + [
        (200, anthropic_message("Koniec."), {})]
    assistant = claude_assistant(stub, max_tool_rounds=2)
    answer = run(assistant.ask("x", state=STATE, tools=FakeTools()))
    assert answer.text == "Koniec." and answer.tool_rounds == 2
    assert ["tools" in r["body"] for r in stub["requests"]] == [True, True, False]


def test_tools_disabled_in_config_put_recalled_concepts_into_the_context(stub):
    assistant = claude_assistant(stub, tools=False)
    tools = FakeTools()
    run(assistant.ask("Co z lampą?", state=STATE, tools=tools))
    body = stub["requests"][0]["body"]
    assert "tools" not in body and tools.context_asked == 1
    assert "Pamięć IRIS, pojęcia przypomniane do tej wypowiedzi:\n- lampa (siła 0,40, wprost)" in body["messages"][0]["content"]


def test_without_recall_in_specs_memory_goes_to_the_context_and_remember_stays(stub):
    tools = FakeTools(specs=(REMEMBER,), context=())  # send_memory nie pozwala: kontekst pusty
    run(claude_assistant(stub).ask("Hej", state=STATE, tools=tools))
    body = stub["requests"][0]["body"]
    assert [t["name"] for t in body["tools"]] == ["iris_remember"] and tools.context_asked == 1
    assert "Pamięć IRIS" not in body["messages"][0]["content"]


def test_unknown_tool_and_tool_errors_become_error_results(stub):
    bad = dict(TOOL_USE, id="toolu_2", name="rm_rf")
    stub["script"] = [
        (200, anthropic_message(content=[bad, TOOL_USE], stop_reason="tool_use"), {}),
        (200, anthropic_message("Nie mogę tego sprawdzić."), {}),
    ]
    run(claude_assistant(stub).ask("x", state=STATE, tools=FakeTools(boom=True)))
    results = stub["requests"][1]["body"]["messages"][2]["content"]
    assert [(r["tool_use_id"], r["is_error"]) for r in results] == [("toolu_2", True), ("toolu_1", True)]
    assert results[0]["content"] == "Nieznane narzędzie: rm_rf." and results[1]["content"] == "Błąd narzędzia."


# ---- Ollama ---------------------------------------------------------------------------------------------


def ollama_reply(content="", tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"model": "qwen", "message": message, "done": True, "done_reason": "stop"}


def ollama(script):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        status, body = script.pop(0) if len(script) > 1 else script[0]
        return httpx.Response(status, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaEngine("qwen", client=client, retries=0), requests


def test_ollama_tool_calls_and_tool_messages():
    engine, requests = ollama([
        (200, ollama_reply(tool_calls=[{"function": {"name": "iris_recall", "arguments": {"query": "pies"}}}])),
        (200, ollama_reply("Twój pies ma na imię Burek.")),
    ])
    assistant = Assistant(engine, LLMConfig(engine="ollama", model="qwen"))
    answer = run(assistant.ask("Jak ma na imię mój pies?", state=STATE, tools=FakeTools()))
    assert answer.text == "Twój pies ma na imię Burek."
    first, second = requests
    assert first["tools"][0] == {"type": "function", "function": {
        "name": "iris_recall", "description": RECALL.description, "parameters": RECALL.parameters}}
    assert second["messages"][-2] == {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "iris_recall", "arguments": {"query": "pies"}}}]}
    assert second["messages"][-1] == {"role": "tool", "content": "lampa (siła 0,62, wprost)", "tool_name": "iris_recall"}


def test_ollama_arguments_as_json_text_and_bad_entries():
    engine, _ = ollama([(200, ollama_reply(tool_calls=[
        {"function": {"name": "iris_recall", "arguments": '{"query": "kot"}'}},
        {"function": {"arguments": {}}},
        "śmieć",
    ]))])
    reply = run(engine.complete("S", [Message("user", "x")], max_tokens=10, tools=[RECALL]))
    assert reply.finish == "tool_use" and reply.tool_calls == (ToolCall("call_0", "iris_recall", {"query": "kot"}),)


def test_ollama_model_without_tools_falls_back_to_context_for_the_rest_of_the_session():
    engine, requests = ollama([
        (400, {"error": 'registry.ollama.ai/library/gemma3 does not support tools'}),
        (200, ollama_reply("Lampa jest w salonie.")),
    ])
    assistant = Assistant(engine, LLMConfig(engine="ollama", model="qwen"))
    tools = FakeTools()
    run(assistant.ask("Gdzie lampa?", state=STATE, tools=tools))
    run(assistant.ask("A teraz?", state=STATE, tools=tools))
    assert ["tools" in r for r in requests] == [True, False, False]
    assert "Pamięć IRIS" in requests[1]["messages"][-1]["content"] and tools.context_asked == 2


def test_ollama_other_400_is_a_plain_error():
    engine, _ = ollama([(400, {"error": "zły format"})])
    with pytest.raises(Exception) as e:
        run(engine.complete("S", [Message("user", "x")], max_tokens=10, tools=[RECALL]))
    assert not isinstance(e.value, LLMToolsUnsupported)


# ---- silnik bez narzędzi --------------------------------------------------------------------------------


class PlainEngine:
    remote = False

    def __init__(self):
        self.seen: list[list[Message]] = []

    async def complete(self, system, messages, *, max_tokens):
        self.seen.append(list(messages))
        return LLMReply("Dobrze.", "stop", "plain")

    async def aclose(self):
        pass


def test_engine_without_tool_support_gets_memory_in_the_context_and_no_tools_argument():
    engine = PlainEngine()
    tools = FakeTools()
    run(Assistant(engine, LLMConfig()).ask("Co z lampą?", state=STATE, tools=tools))
    assert "- lampa (siła 0,40, wprost)" in engine.seen[0][-1].content and tools.context_asked == 1


def test_without_toolbox_there_is_no_memory_at_all():
    engine = PlainEngine()
    run(Assistant(engine, LLMConfig()).ask("Co z lampą?", state=STATE))
    assert "Pamięć IRIS" not in engine.seen[0][-1].content


def test_tool_spec_schemas_are_closed():
    for spec in (RECALL, REMEMBER):
        assert isinstance(spec, ToolSpec) and spec.parameters["additionalProperties"] is False
    assert REMEMBER.parameters["properties"]["domain"]["enum"] == ["dom", "czas", "ty", "rozmowa"]


# ---- pojęcia z wypowiedzi -------------------------------------------------------------------------------


class ScriptedEngine(PlainEngine):
    def __init__(self, text, finish="stop"):
        super().__init__()
        self.text, self.finish = text, finish

    async def complete(self, system, messages, *, max_tokens):
        self.seen.append([Message("user", system), *messages])
        return LLMReply(self.text, self.finish, "plain")


def test_extract_concepts_sends_only_the_utterance_and_parses_the_reply():
    from host.brain.learn import CONCEPTS_PROMPT, LearnError, extract_concepts
    from host.core.config import MemoryConfig

    engine = ScriptedEngine('```json\n{"domain": "ty", "concepts": ["pies", "Burek"]}\n```')
    ext = run(extract_concepts(engine, "Mój pies to Burek", MemoryConfig()))
    assert ext.domain == "ty" and [l for _, l in ext.concepts] == ["pies", "Burek"] and ext.source == "llm"
    system, utterance = engine.seen[0]
    assert system.content == CONCEPTS_PROMPT and utterance.content == "Mój pies to Burek"
    with pytest.raises(LearnError):
        run(extract_concepts(ScriptedEngine("nie wiem"), "x", MemoryConfig()))
    with pytest.raises(LearnError):
        run(extract_concepts(ScriptedEngine("{}", finish="refusal"), "x", MemoryConfig()))
