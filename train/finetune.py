"""Dostrajanie modelu bazowego metodą LoRA na zbiorze IRIS (docs/spec.md, sekcja 15, "Dostrajanie").

    pip install -r train/requirements.txt
    python -m train.finetune --base speakleash/Bielik-4.5B-v3.0-Instruct --data train/data/iris.jsonl \
        --out train/out/iris-lora --merge

Wymaga karty graficznej (kilka do kilkunastu GB pamięci, zależnie od modelu bazowego). Biblioteki treningowe
są importowane dopiero w main(), więc przygotowanie przykładów (prepare) działa i jest testowane bez nich.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("train.finetune")

IGNORE = -100  # etykieta pomijana w funkcji straty


class ChatTokenizer(Protocol):
    def apply_chat_template(self, conversation: list[dict[str, Any]], **kwargs: Any) -> Any: ...

    def __call__(self, text: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class Prepared:
    input_ids: list[int]
    labels: list[int]  # IGNORE na rozmowie przed odpowiedzią: model uczy się tylko własnej odpowiedzi


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def template_supports_tools(tokenizer: ChatTokenizer, rows: list[dict[str, Any]]) -> bool:
    """Czy szablon rozmowy modelu bazowego wstawia definicje narzędzi (inaczej tryb narzędzi nie ma sensu)."""
    row = next((r for r in rows if r["tools"] and len(r["messages"]) == 3), None)  # system, user, odpowiedź
    if row is None:
        return False
    head = row["messages"][:-1]
    try:
        with_tools = tokenizer.apply_chat_template(head, tools=row["tools"], tokenize=False, add_generation_prompt=True)
        without = tokenizer.apply_chat_template(head, tokenize=False, add_generation_prompt=True)
    except Exception as e:  # noqa: BLE001 - szablony zgłaszają własne wyjątki (jinja2)
        log.warning("szablon rozmowy odrzuca narzędzia: %s", e)
        return False
    return bool(with_tools != without)


def prepare(rows: list[dict[str, Any]], tokenizer: ChatTokenizer, *, max_tokens: int) -> tuple[list[Prepared], dict[str, int]]:
    """Przykłady w tokenach. Pomija wiersze, których szablon nie umie złożyć, i dłuższe niż `max_tokens`."""
    tools_ok = template_supports_tools(tokenizer, rows)
    out: list[Prepared] = []
    skipped = {"tools_unsupported": 0, "template_error": 0, "too_long": 0}
    for row in rows:
        uses_tools = bool(row["tools"]) or any(m["role"] == "tool" or m.get("tool_calls") for m in row["messages"])
        if uses_tools and not tools_ok:
            skipped["tools_unsupported"] += 1
            continue
        kwargs: dict[str, Any] = {"tools": row["tools"]} if row["tools"] else {}
        try:
            prefix = tokenizer.apply_chat_template(row["messages"][:-1], tokenize=False, add_generation_prompt=True, **kwargs)
            full = tokenizer.apply_chat_template(row["messages"], tokenize=False, **kwargs)
        except Exception:  # noqa: BLE001
            skipped["template_error"] += 1
            continue
        if not full.startswith(prefix):
            skipped["template_error"] += 1  # szablon składa odpowiedź inaczej niż początek generowania
            continue
        ids = list(tokenizer(full, add_special_tokens=False)["input_ids"])
        n_prefix = len(tokenizer(prefix, add_special_tokens=False)["input_ids"])
        if len(ids) > max_tokens or n_prefix >= len(ids):
            skipped["too_long" if len(ids) > max_tokens else "template_error"] += 1
            continue
        out.append(Prepared(ids, [IGNORE] * n_prefix + ids[n_prefix:]))
    return out, {k: v for k, v in skipped.items() if v}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="model bazowy: nazwa z Hugging Face albo katalog")
    parser.add_argument("--data", type=Path, required=True, help="zbiór z python -m train.dataset")
    parser.add_argument("--out", type=Path, required=True, help="katalog na adapter LoRA")
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--accumulate", type=int, default=4)
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--holdout", type=float, default=0.05, help="część przykładów do pomiaru straty")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--merge", action="store_true", help="zapisz też pełny model z wtopionym adapterem (do GGUF)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    import random

    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments

    tokenizer = AutoTokenizer.from_pretrained(args.base)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    rows = load_rows(args.data)
    examples, skipped = prepare(rows, tokenizer, max_tokens=args.max_tokens)
    log.info("przykłady: %d z %d wierszy; pominięte: %s", len(examples), len(rows), skipped or "brak")
    if skipped.get("tools_unsupported"):
        log.warning("szablon modelu bazowego nie obsługuje narzędzi: model nauczy się tylko trybu z pamięcią "
                    "w bloku kontekstu (w hoście ustaw brain.llm.tools = false)")
    if not examples:
        raise SystemExit("brak przykładów do treningu")
    random.Random(args.seed).shuffle(examples)
    n_eval = int(len(examples) * args.holdout) if len(examples) >= 40 else 0
    eval_set, train_set = examples[:n_eval], examples[n_eval:]

    cuda = torch.cuda.is_available()
    model = AutoModelForCausalLM.from_pretrained(args.base, torch_dtype=torch.bfloat16 if cuda else torch.float32,
                                                 device_map="auto" if cuda else None)
    model = get_peft_model(model, LoraConfig(r=args.rank, lora_alpha=2 * args.rank, lora_dropout=0.05,
                                             target_modules="all-linear", task_type="CAUSAL_LM"))
    model.print_trainable_parameters()

    def collate(batch: list[Prepared]) -> dict[str, Any]:
        width = max(len(b.input_ids) for b in batch)
        pad = tokenizer.pad_token_id
        return {
            "input_ids": torch.tensor([b.input_ids + [pad] * (width - len(b.input_ids)) for b in batch]),
            "attention_mask": torch.tensor([[1] * len(b.input_ids) + [0] * (width - len(b.input_ids)) for b in batch]),
            "labels": torch.tensor([b.labels + [IGNORE] * (width - len(b.labels)) for b in batch]),
        }

    # Rozgrzewka 5% kroków: transformers 4 ma `warmup_ratio`, transformers 5 przyjmuje ułamek w `warmup_steps`.
    import inspect

    ratio = "warmup_ratio" in inspect.signature(TrainingArguments.__init__).parameters
    warmup: dict[str, Any] = {"warmup_ratio": 0.05} if ratio else {"warmup_steps": 0.05}
    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(args.out), num_train_epochs=args.epochs, learning_rate=args.lr,
            per_device_train_batch_size=args.batch, gradient_accumulation_steps=args.accumulate,
            lr_scheduler_type="cosine", logging_steps=10, save_strategy="no", **warmup,
            eval_strategy="epoch" if eval_set else "no", report_to="none", seed=args.seed,
            bf16=cuda, use_cpu=not cuda,
        ),
        train_dataset=train_set, eval_dataset=eval_set or None, data_collator=collate,
    )
    trainer.train()
    model.save_pretrained(str(args.out))
    tokenizer.save_pretrained(str(args.out))
    log.info("adapter zapisany w %s", args.out)
    if args.merge:
        merged = args.out / "merged"
        model.merge_and_unload().save_pretrained(str(merged))
        tokenizer.save_pretrained(str(merged))
        log.info("pełny model zapisany w %s", merged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
