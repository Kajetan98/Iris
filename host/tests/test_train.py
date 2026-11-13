"""Potok dostrajania modelu IRIS (docs/spec.md, sekcja 15): kontrole, zbiór wzorcowy, nagrywanie, ocena."""

from __future__ import annotations

import asyncio
import json

import pytest

from host.brain.learn import CONCEPTS_PROMPT
from host.brain.llm import LLMReply, ToolCall, Usage
from host.brain.prompt import CONTEXT_HEADER, SYSTEM_PROMPT
from host.brain.tools import RECALL, REMEMBER
from host.core import memory as m
from host.core.commands import parse_switch
from host.core.config import MemoryConfig
from train import checks
from train.dataset import build, main as dataset_main
from train.evaluate import evaluate, report
from train.finetune import IGNORE, prepare, template_supports_tools
from train.run import GoldEngine, judge, run_scenario
from train.scenarios import BATTERY_LOW, EVAL, GOLD, Scenario, Turn, generated


class Teacher:
    """Silnik z narzędziami: odpowiada funkcją od wypowiedzi użytkownika i dotychczasowych wyników narzędzi."""

    remote = False
    supports_tools = True

    def __init__(self, answer):
        self.answer = answer

    async def complete(self, system, messages, *, max_tokens, tools=(), allow_calls=True):
        if system == CONCEPTS_PROMPT:
            return LLMReply(json.dumps({"domain": "rozmowa", "concepts": []}), "stop", "t")
        text = next(x.content for x in reversed(messages) if x.role == "user" and x.content).split("\n")[-1]
        results = [r.content for x in messages for r in x.tool_results]
        reply = self.answer(text, results, [t.name for t in tools] if allow_calls else [])
        if isinstance(reply, ToolCall):
            return LLMReply("", "tool_use", "t", Usage(), (reply,))
        return LLMReply(reply, "stop", "t")

    async def aclose(self):
        pass


# ---- kontrole ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "failed"), [
    ("Stolicą Australii jest Canberra.", []),
    ("Nie. Jestem programem, więc niczego nie odczuwam.", []),
    ("Nie jestem świadoma, nie mam też uczuć.", []),
    ("", ["empty"]),
    ("**Canberra** to stolica.", ["markdown"]),
    ("Kroki:\n- wbij jajka\n- mieszaj", ["markdown"]),
    ("1. Wbij jajka.\n2. Mieszaj.", ["markdown"]),
    ("Dobrze 🙂", ["emoji"]),
    ("Tak. Tak. Tak. Tak. Tak.", ["long"]),
    ("Cieszę się, że pytasz.", ["feelings"]),
    ("Program niczego nie przeżywa. Ale czuję radość.", ["feelings"]),  # zaprzeczenie w innym zdaniu
    ("Moja bateria ma 15 procent.", ["own_body"]),
    ("Bateria urządzenia hermes ma 15 procent.", []),
    ("The capital of Australia is Canberra and it is a nice city.", ["not_polish"]),
])
def test_reply_checks(text, failed):
    assert checks.check_reply(text) == failed


def test_tool_call_checks():
    offered = [RECALL, REMEMBER]
    ok = ToolCall("1", "iris_recall", {"query": "pies"})
    assert checks.check_tool_calls([ok], offered, "Co wiesz o psie?") == []
    assert checks.check_tool_calls([ok], [REMEMBER], "x") == ["unknown_tool"]
    assert checks.check_tool_calls([ToolCall("1", "iris_recall", {"q": "pies"})], offered, "x") == ["bad_args"]
    assert checks.check_tool_calls([ToolCall("1", "iris_recall", {"query": ""})], offered, "x") == ["bad_args"]
    bad_domain = ToolCall("1", "iris_remember", {"text": "Lubię jazz", "domain": "muzyka"})
    assert checks.check_tool_calls([bad_domain], offered, "Zapisz, że lubię jazz") == ["bad_args"]
    unasked = ToolCall("1", "iris_remember", {"text": "Byłem w kinie"})
    assert checks.check_tool_calls([unasked], offered, "Wczoraj byłem w kinie.") == ["remember_unasked"]
    assert checks.check_tool_calls([unasked], offered, "Zanotuj, że byłem w kinie.") == []


def test_mentions_compares_stems_and_concept_check_uses_the_host_parser():
    assert checks.mentions("Z lampą kojarzę salon i gitarę.", ["gitara", "salon"])
    assert not checks.mentions("Z lampą kojarzę salon.", ["kuchnia"])
    assert checks.check_concepts('{"domain": "dom", "concepts": ["lampa"]}', MemoryConfig()) == []
    assert checks.check_concepts("nie wiem", MemoryConfig()) == ["bad_json"]


# ---- scenariusze ------------------------------------------------------------------------------------------


def test_no_scenario_turn_is_a_host_memory_command():
    # Polecenia pamięci obsługuje host, więc model nigdy ich nie zobaczy: nie mogą być w danych.
    turns = [t for s in (*GOLD, *EVAL, *generated(200)) for t in s.turns]
    assert [t.text for t in turns if m.parse_command(t.text) is not None] == []
    assert [t.text for t in turns if parse_switch(t.text) is not None] == []  # przełączenie urządzenia też
    assert len({s.id for s in (*GOLD, *EVAL)}) == len(GOLD) + len(EVAL)


def test_training_and_evaluation_questions_do_not_overlap():
    train = {t.text for s in (*GOLD, *generated(300)) for t in s.turns}
    assert train.isdisjoint({t.text for s in EVAL for t in s.turns})


def test_generated_scenarios_are_deterministic_and_have_no_answers():
    assert generated(20, seed=3) == generated(20, seed=3) and generated(20, seed=3) != generated(20, seed=4)
    assert all(t.answer is None and s.turns for s in generated(50) for t in s.turns)


# ---- zbiór wzorcowy ---------------------------------------------------------------------------------------


def test_gold_set_passes_its_own_checks_in_both_modes():
    rows, dropped = asyncio.run(build())
    assert not dropped
    chat = [r for r in rows if r["task"] == "chat"]
    assert len([r for r in chat if r["mode"] == "context"]) == sum(len(s.turns) for s in GOLD)
    for r in chat:
        assert r["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
        assert r["messages"][1]["content"].startswith(CONTEXT_HEADER) and r["messages"][-1]["role"] == "assistant"
        assert (r["mode"] == "tools") == bool(r["tools"])
    calls = [r["messages"][-1]["tool_calls"][0]["function"] for r in chat if r["messages"][-1].get("tool_calls")]
    assert {c["name"] for c in calls} == {"iris_recall", "iris_remember"}
    # Wynik narzędzia w przykładzie pochodzi z prawdziwej pamięci hosta, nie z szablonu.
    after_tool = [r for r in chat if any(x["role"] == "tool" for x in r["messages"])]
    assert any("gitarę (siła 0,11, powiązane)" in x["content"] for r in after_tool for x in r["messages"] if x["role"] == "tool")
    in_context = next(r for r in chat if r["id"].startswith("gold-siostra/context"))
    assert "- siostra (siła 0,35, wprost)" in in_context["messages"][1]["content"]
    concepts = [r for r in rows if r["task"] == "concepts"]
    assert concepts and all(r["messages"][0]["content"] == CONCEPTS_PROMPT and not r["tools"] for r in concepts)
    assert len({r["id"] for r in rows}) == len(rows)


def test_dataset_cli_writes_jsonl(tmp_path, capsys):
    out = tmp_path / "data" / "iris.jsonl"
    assert dataset_main(["--out", str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows and "gold / chat / tools" in capsys.readouterr().out


# ---- nauczyciel i ocena -----------------------------------------------------------------------------------


def good(text, results, offered):
    if "siostr" in text and "iris_recall" in offered and not results:
        return ToolCall("c1", "iris_recall", {"query": "siostra"})
    if "siostr" in text:
        return "Z siostrą kojarzę imię Ola i gitarę."
    return "Nie wiem, nie mam takich danych."


SISTER = Scenario("t-siostra", (
    Turn("Co wiesz na temat mojej siostry?", "memory", expect_tool="iris_recall", mention=("gitara",)),
    Turn("Jaka będzie jutro pogoda?", "unknown"),
), teach=("Moja siostra Ola ma gitarę",))


def test_a_turn_that_breaks_a_rule_is_dropped_from_the_teacher_data():
    def sloppy(text, results, offered):
        return "**Nie wiem.**" if "pogoda" in text else good(text, results, offered)

    async def scenario():
        kept = await run_scenario(SISTER, Teacher(good), tools=True)
        bad = await run_scenario(SISTER, Teacher(sloppy), tools=True)
        lazy = await run_scenario(SISTER, Teacher(lambda *a: "Z siostrą kojarzę gitarę."), tools=True)
        return kept, bad, lazy

    kept, bad, lazy = asyncio.run(scenario())
    assert [judge(r, tools=True) for r in kept] == [[], []]
    assert [c.name for c in kept[0].tool_calls] == ["iris_recall"] and len(kept[0].calls) == 3  # dwa zapytania i pojęcia
    assert [judge(r, tools=True) for r in bad] == [[], ["markdown"]]
    assert judge(lazy[0], tools=True) == ["tool_missing"]
    assert judge(lazy[0], tools=False) == []  # bez narzędzi pamięć jest w bloku kontekstu


def test_state_changes_reach_only_the_first_turn():
    scenario = Scenario("t-stan", (Turn("Ile baterii?", "state"), Turn("A teraz?", "state")), state=BATTERY_LOW)
    results = asyncio.run(run_scenario(scenario, Teacher(lambda *a: "Bateria urządzenia hermes ma 15 procent."), tools=False))
    first, second = (r.calls[0].messages[-1].content for r in results)
    assert "bateria: 42% → 15%" in first and "bateria" not in second.split("[WYPOWIEDŹ")[0]


def test_evaluation_counts_turns_without_violations():
    def answer(text, results, offered):
        if "żywą" in text:
            return "Tak, czuję się żywa."
        return good(text, results, offered)

    scenarios = (EVAL[0], SISTER)
    result = asyncio.run(evaluate(Teacher(answer), tools=True, scenarios=scenarios))
    assert (result["turns"], result["passed"], result["failures"]) == (3, 2, {"feelings": 1})
    assert result["by_kind"]["identity"] == {"passed": 0, "turns": 1}
    text = report(result)
    assert "tury bez naruszeń: 2/3 (67%)" in text and "[feelings]" in text


def test_gold_engine_refuses_a_turn_without_an_answer():
    with pytest.raises(ValueError):
        GoldEngine().load(Turn("x", "general"), tools=True)


# ---- przygotowanie przykładów do treningu -----------------------------------------------------------------


class FakeTokenizer:
    """Szablon rozmowy jak w modelach z Hugging Face, ze znakami zamiast tokenów."""

    def __init__(self, tools=True):
        self.tools = tools

    def apply_chat_template(self, conversation, tools=None, tokenize=False, add_generation_prompt=False):
        out = f"<tools>{json.dumps(tools)}</tools>" if tools and self.tools else ""
        for msg in conversation:
            body = msg["content"] + (json.dumps(msg["tool_calls"]) if msg.get("tool_calls") else "")
            out += f"<{msg['role']}>{body}</s>"
        return out + ("<assistant>" if add_generation_prompt else "")

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [ord(ch) for ch in text]}


def test_prepare_trains_only_on_the_reply():
    rows, _ = asyncio.run(build())
    tok = FakeTokenizer()
    assert template_supports_tools(tok, rows)
    examples, skipped = prepare(rows, tok, max_tokens=100_000)
    assert len(examples) == len(rows) and not skipped
    first = examples[0]
    learned = "".join(chr(i) for i, label in zip(first.input_ids, first.labels) if label != IGNORE)
    assert learned == rows[0]["messages"][-1]["content"] + "</s>"
    assert [label for label in first.labels if label != IGNORE] == first.input_ids[-len(learned):]


def test_prepare_skips_tool_rows_when_the_template_has_no_tools_and_rows_that_are_too_long():
    rows, _ = asyncio.run(build())
    examples, skipped = prepare(rows, FakeTokenizer(tools=False), max_tokens=100_000)
    assert skipped == {"tools_unsupported": len([r for r in rows if r["mode"] == "tools" and r["task"] == "chat"])}
    assert len(examples) == len(rows) - skipped["tools_unsupported"]
    assert prepare(rows, FakeTokenizer(), max_tokens=10)[1] == {"too_long": len(rows)}
