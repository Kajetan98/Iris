"""Ocena modelu na turach spoza zbioru treningowego (docs/spec.md, sekcja 15, "Ocena").

    python -m train.evaluate --config iris.toml            # model z narzędziami pamięci
    python -m train.evaluate --config iris.toml --no-tools # pamięć w bloku kontekstu
    python -m train.evaluate --config iris.toml --json wynik.json

Wynik to odsetek tur bez naruszeń i liczba naruszeń każdej reguły. Uruchom przed dostrojeniem (model
bazowy) i po nim: model dostrojony ma sens tylko wtedy, gdy wynik jest wyraźnie lepszy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from host.brain import build_engine
from host.brain.llm import LLMEngine
from host.core.config import LLMConfig
from train.run import judge, run_scenario
from train.scenarios import EVAL, Scenario


def engine_from(path: Path) -> LLMEngine:
    """Silnik z sekcji [brain.llm] pliku TOML (ten sam format co konfiguracja hosta)."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    engine = build_engine(LLMConfig.model_validate(data.get("brain", {}).get("llm", {})))
    if engine is None:
        raise SystemExit(f"{path}: sekcja [brain.llm] nie wybiera silnika")
    return engine


async def evaluate(engine: LLMEngine, *, tools: bool, scenarios: tuple[Scenario, ...] = EVAL) -> dict[str, Any]:
    turns: list[dict[str, Any]] = []
    for scenario in scenarios:
        for i, result in enumerate(await run_scenario(scenario, engine, tools=tools)):
            turns.append({"id": f"{scenario.id}/{i}", "kind": result.turn.kind, "text": result.turn.text,
                          "answer": result.answer, "tools": [c.name for c in result.tool_calls],
                          "failed": judge(result, tools=tools)})
    total = len(turns)
    passed = sum(not t["failed"] for t in turns)
    by_kind: dict[str, list[int]] = {}
    for t in turns:
        ok, n = by_kind.setdefault(t["kind"], [0, 0])
        by_kind[t["kind"]] = [ok + (not t["failed"]), n + 1]
    return {"mode": "tools" if tools else "context", "turns": total, "passed": passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "failures": dict(Counter(f for t in turns for f in t["failed"]).most_common()),
            "by_kind": {k: {"passed": v[0], "turns": v[1]} for k, v in sorted(by_kind.items())},
            "details": turns}


def report(result: dict[str, Any]) -> str:
    lines = [f"tryb: {result['mode']}, tury bez naruszeń: {result['passed']}/{result['turns']} "
             f"({result['pass_rate'] * 100:.0f}%)"]
    lines += [f"  {kind}: {v['passed']}/{v['turns']}" for kind, v in result["by_kind"].items()]
    if result["failures"]:
        lines.append("naruszenia: " + ", ".join(f"{k}: {v}" for k, v in result["failures"].items()))
    for t in result["details"]:
        if t["failed"]:
            lines.append(f"- {t['id']} [{', '.join(t['failed'])}] {t['text']!r} -> {t['answer']!r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True, help="plik TOML z sekcją [brain.llm]")
    parser.add_argument("--no-tools", action="store_true", help="pamięć w bloku kontekstu zamiast narzędzi")
    parser.add_argument("--json", type=Path, help="zapisz pełny wynik do pliku")
    args = parser.parse_args(argv)

    async def run() -> dict[str, Any]:
        engine = engine_from(args.config)
        try:
            return await evaluate(engine, tools=not args.no_tools)
        finally:
            await engine.aclose()

    result = asyncio.run(run())
    print(report(result))
    if args.json is not None:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
