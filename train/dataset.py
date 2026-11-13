"""Zbiór treningowy modelu IRIS w formacie rozmów JSONL (docs/spec.md, sekcja 15, "Zbiór danych").

    python -m train.dataset --out train/data/iris.jsonl
    python -m train.dataset --out train/data/iris.jsonl --teacher-config nauczyciel.toml --n 500

Bez nauczyciela zbiór zawiera tylko przykłady wzorcowe (GOLD). Z nauczycielem (dowolny silnik z sekcji
`[brain.llm]` wskazanego pliku) dochodzą odpowiedzi nauczyciela na scenariusze losowane, odsiane kontrolami.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from host.brain.llm import LLMEngine, Message, ToolSpec
from host.core.config import MemoryConfig
from train import checks
from train.run import Call, GoldEngine, TurnResult, judge, run_scenario
from train.scenarios import GOLD, Scenario, generated


def tool_json(spec: ToolSpec) -> dict[str, Any]:
    return {"type": "function",
            "function": {"name": spec.name, "description": spec.description, "parameters": spec.parameters}}


def message_json(message: Message) -> list[dict[str, Any]]:
    """Wiadomość hosta w formacie szablonów rozmowy: wywołania przy asystencie, wyniki jako role `tool`."""
    if message.tool_results:
        return [{"role": "tool", "name": r.name, "content": r.content} for r in message.tool_results]
    out: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        out["tool_calls"] = [{"type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                             for c in message.tool_calls]
    return [out]


def example(call: Call, *, id_: str, source: str, mode: str) -> dict[str, Any]:
    """Jeden przykład: rozmowa do tego zapytania i odpowiedź modelu jako ostatnia wiadomość."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": call.system}]
    for m in call.messages:
        messages += message_json(m)
    messages += message_json(Message("assistant", call.reply.text, tool_calls=call.reply.tool_calls))
    return {"id": id_, "task": call.task, "source": source, "mode": mode,
            "tools": [tool_json(t) for t in call.tools], "messages": messages}


def examples_from(scenario: Scenario, results: list[TurnResult], *, source: str, tools: bool,
                  dropped: Counter[str]) -> list[dict[str, Any]]:
    """Przykłady z tur, które przeszły kontrole. Tura z naruszeniem odpada w całości."""
    mode = "tools" if tools else "context"
    cfg = MemoryConfig()
    out: list[dict[str, Any]] = []
    for t, result in enumerate(results):
        failed = judge(result, tools=tools)
        if failed:
            dropped.update(failed)
            continue
        for c, call in enumerate(result.calls):
            if call.task == "concepts" and (not call.reply.text.strip() or checks.check_concepts(call.reply.text, cfg)):
                continue  # pusta lub zła odpowiedź o pojęciach: host użył heurystyki, przykład pomijamy
            out.append(example(call, id_=f"{scenario.id}/{mode}/{t}.{c}", source=source, mode=mode))
    return out


async def build(teacher: LLMEngine | None = None, *, n: int = 0, seed: int = 1) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Zbiór: przykłady wzorcowe w obu trybach i, z nauczycielem, `n` scenariuszy losowanych."""
    rows: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    for tools in (True, False):
        for scenario in GOLD:
            results = await run_scenario(scenario, GoldEngine(), tools=tools)
            rows += examples_from(scenario, results, source="gold", tools=tools, dropped=dropped)
    if teacher is not None:
        for scenario in generated(n, seed):
            for tools in (True, False):
                results = await run_scenario(scenario, teacher, tools=tools)
                rows += examples_from(scenario, results, source="teacher", tools=tools, dropped=dropped)
    return rows, dropped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--teacher-config", type=Path, help="plik TOML z sekcją [brain.llm] modelu-nauczyciela")
    parser.add_argument("--n", type=int, default=300, help="liczba scenariuszy dla nauczyciela")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    teacher = None
    if args.teacher_config is not None:
        from train.evaluate import engine_from

        teacher = engine_from(args.teacher_config)
    rows, dropped = asyncio.run(build(teacher, n=args.n, seed=args.seed))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    by = Counter((r["source"], r["task"], r["mode"]) for r in rows)
    print(f"zapisano {len(rows)} przykładów do {args.out}")
    for key, count in sorted(by.items()):
        print(f"  {' / '.join(key)}: {count}")
    if dropped:
        print("odrzucone tury według reguły: " + ", ".join(f"{k}: {v}" for k, v in dropped.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
