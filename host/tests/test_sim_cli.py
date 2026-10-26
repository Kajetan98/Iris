from pathlib import Path

import pytest

from sim.body import build_config, main, parse_args
from sim.model import BodyModel, parse_script

SCENARIOS = sorted((Path(__file__).parents[2] / "sim" / "scenarios").glob("*.txt"))


def test_defaults():
    args = parse_args([])
    assert args.url == "ws://localhost:8000/ws"
    cfg = build_config(args)
    assert (cfg.batt_pct, cfg.drain_per_min, cfg.heartbeat_interval_s) == (71.0, 0.0, 1.0)
    assert cfg.gpio_pins == frozenset({5, 6, 7})


def test_options_reach_config():
    args = parse_args(
        ["--batt", "15", "--drain-per-min", "2", "--gpio-pins", "1,2", "--buffer-limit", "8", "--seed", "9"]
    )
    cfg = build_config(args)
    assert (cfg.batt_pct, cfg.drain_per_min, cfg.buffer_limit, cfg.seed) == (15.0, 2.0, 8, 9)
    assert cfg.gpio_pins == frozenset({1, 2})


def test_bad_script_exits_with_2(tmp_path, capsys):
    path = tmp_path / "bad.txt"
    path.write_text("abc press\n")
    assert main(["--script", str(path), "--no-console"]) == 2
    assert "linia 1" in capsys.readouterr().err
    assert main(["--script", str(tmp_path / "nie-ma.txt"), "--no-console"]) == 2


def test_scenarios_exist():
    assert {p.name for p in SCENARIOS} >= {"low_battery.txt", "link_loss.txt"}


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.name)
def test_scenario_files_parse_and_use_known_commands(path):
    steps = parse_script(path.read_text(encoding="utf-8"))
    assert steps
    model = BodyModel(build_config(parse_args([])), 0.0)
    for _, command in steps:
        reply = model.command(command, 0.0)
        assert reply is None or not reply.startswith(("błąd", "nieznana")), (command, reply)
