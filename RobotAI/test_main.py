"""Testy main.py: python -m pytest test_main.py (wymaga bibliotek z komentarza na początku main.py).

Model Gemma nie jest potrzebny: silnik LiteRT-LM i model zastępują atrapy o tym samym interfejsie.
"""

from __future__ import annotations

import itertools
import time
from pathlib import Path

import gymnasium as gym
import numpy as np
import pytest
from gymnasium.utils.env_checker import check_env

import main as m


@pytest.fixture
def env():
    e = m.HumanlikeGaitReward(gym.make(m.ENV_ID))
    yield e
    e.close()


def test_environment_follows_the_gymnasium_api():
    e = gym.make(m.ENV_ID)
    try:
        check_env(e.unwrapped, skip_render_check=True)
        assert e.observation_space.shape == (20,) and e.action_space.shape == (6,)
        assert e.spec.max_episode_steps == m.MAX_EPISODE_STEPS
    finally:
        e.close()


def test_without_torque_the_robot_falls_and_the_episode_ends(env):
    env.reset(seed=0)
    for step in range(300):
        _, reward, terminated, truncated, info = env.step(np.zeros(6))
        if terminated:
            break
    assert terminated and info["fallen"] and step < 300
    assert info["reward_terms"]["fall"] == -env.weights.fall


def test_reward_velocities_come_from_the_displacement_in_each_control_step(env):
    # Chwilowa prędkość z końca kroku dawała się oszukać drganiem w miejscu (nagroda bez postępu).
    _, info = env.reset(seed=0)
    assert info["forward_velocity"] == 0.0 and info["vertical_velocity"] == 0.0
    dt = env.unwrapped.dt
    rng = np.random.default_rng(0)
    for _ in range(60):
        x_before, height_before = info["x_position"], info["torso_height"]
        _, _, terminated, _, info = env.step(rng.uniform(-1.0, 1.0, 6))
        assert info["forward_velocity"] == pytest.approx((info["x_position"] - x_before) / dt)
        assert info["vertical_velocity"] == pytest.approx((info["torso_height"] - height_before) / dt)
        if terminated:
            break


def test_reward_terms_follow_the_formula(env):
    w = env.weights
    info = {
        "action": np.array([0.5, -0.5, 0.0, 0.0, 1.0, -1.0]),
        "joint_velocities": np.array([2.0, 2.0, 0.0, 0.0, 1.0, 1.0]),
        "forward_velocity": 3.0,  # powyżej v_max: nagroda ucięta
        "pitch": 0.2,
        "pitch_rate": -1.0,
        "vertical_velocity": 0.5,
        "torso_height": 1.1,
        "feet_contacts": np.array([0.0, 0.0]),
        "fallen": False,
    }
    previous = np.zeros(6)
    reward, terms = env.compute(info, previous)
    a = info["action"]
    assert terms["forward"] == pytest.approx(w.forward * w.target_speed)
    assert terms["tilt"] == pytest.approx(-w.tilt * 0.04)
    assert terms["tilt_rate"] == pytest.approx(-w.tilt_rate * 1.0)
    assert terms["bob"] == pytest.approx(-w.bob * 0.25)
    assert terms["height"] == pytest.approx(-w.height * (1.1 - w.reference_height) ** 2)
    assert terms["energy"] == pytest.approx(-w.energy * np.mean(a**2))
    assert terms["power"] == pytest.approx(-w.power * np.mean(np.abs(a * info["joint_velocities"])))
    assert terms["smoothness"] == pytest.approx(-w.smoothness * np.mean(a**2))
    assert terms["airborne"] == -w.airborne and terms["fall"] == 0.0 and terms["alive"] == w.alive
    assert reward == pytest.approx(sum(terms.values()))
    # Ta sama akcja co poprzednio: brak kary za szarpanie.
    assert env.compute(info, a)[1]["smoothness"] == 0.0


def test_posture_controller_keeps_the_robot_standing(env):
    controller = m.PostureController()
    for seed in range(3):
        _, info = env.reset(seed=seed)
        for _ in range(900):  # 15 s
            _, _, terminated, _, info = env.step(controller.action(info))
            assert not terminated, f"upadek przy seed={seed}"
        assert abs(info["pitch"]) < 0.3 and info["torso_height"] > 1.1


def test_state_prompt_describes_the_robot():
    info = {
        "pitch": 0.35, "pitch_rate": -0.5, "torso_height": 1.12, "forward_velocity": 0.42,
        "x_position": 3.21, "feet_contacts": np.array([1.0, 0.0]),
    }
    text = m.state_to_prompt(info)
    assert "torso height: 1.12 m" in text and "+20 degrees (forward" in text
    assert "forward speed: +0.42 m/s" in text and "one foot on the ground" in text
    assert "distance walked: 3.2 m" in text


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"command": "walk"}', ("walk", True)),
        ('```json\n{"command": "STAND"}\n```', ("stand", True)),
        ("The robot should walk now.", ("walk", True)),
        ('{"command": "fly"}', ("stand", False)),
        ("banana", ("stand", False)),
        ("", ("stand", False)),
    ],
)
def test_parse_command(reply, expected):
    assert m.parse_command(reply) == expected


class _FakeConversation:
    def __init__(self, engine, kwargs):
        self.engine, self.kwargs = engine, kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_message(self, prompt, **kwargs):
        self.engine.calls.append((self.kwargs, prompt, kwargs))
        if "response_format" in kwargs and self.engine.reject_schema:
            raise RuntimeError("schemat nieobsługiwany")
        return self.engine.reply  # str(odpowiedź) daje tekst, jak litert_lm.Message


class _FakeEngine:
    def __init__(self, reply, reject_schema=False):
        self.reply, self.reject_schema, self.calls = reply, reject_schema, []

    def create_conversation(self, **kwargs):
        return _FakeConversation(self, kwargs)

    def close(self):
        pass


def _brain_with(engine):
    litert_lm = pytest.importorskip("litert_lm")
    brain = object.__new__(m.GemmaBrain)  # bez wczytywania prawdziwego modelu
    brain._lm, brain._engine, brain._constrained, brain._max_output_tokens = litert_lm, engine, True, 32
    return brain


def test_gemma_brain_asks_with_a_json_schema_and_the_system_prompt():
    engine = _FakeEngine('{"command": "walk"}')
    decision = _brain_with(engine).decide("Robot state: ...")
    assert decision.command == "walk" and decision.parsed
    conversation_kwargs, prompt, message_kwargs = engine.calls[0]
    assert conversation_kwargs["system_message"] == m.SYSTEM_PROMPT
    assert conversation_kwargs["max_output_tokens"] == 32
    assert "response_format" in message_kwargs and prompt == "Robot state: ..."


def test_gemma_brain_falls_back_to_plain_text_when_the_schema_is_rejected():
    engine = _FakeEngine("stand", reject_schema=True)
    brain = _brain_with(engine)
    assert brain.decide("x").command == "stand"
    assert brain.decide("y").command == "stand"
    assert len(engine.calls) == 3  # próba ze schematem tylko raz
    assert "response_format" not in engine.calls[-1][2]


def test_gemma_brain_needs_the_model_file(tmp_path):
    pytest.importorskip("litert_lm")
    with pytest.raises(FileNotFoundError):
        m.GemmaBrain(tmp_path / "gemma-4-E2B-it.litertlm")


def test_async_decider_does_not_block_and_turns_errors_into_stand():
    class Slow:
        def decide(self, prompt):
            time.sleep(0.2)
            if prompt == "boom":
                raise RuntimeError("model padł")
            return m.Decision("walk", prompt, 0.2, True)

    decider = m.AsyncDecider(Slow())
    try:
        assert decider.request("a") and not decider.request("b")  # drugie zapytanie czeka
        assert decider.poll() is None
        time.sleep(0.3)
        assert decider.poll().command == "walk"
        decider.request("boom")
        time.sleep(0.3)
        decision = decider.poll()
        assert decision.command == "stand" and not decision.parsed and "model padł" in decision.raw
    finally:
        decider.close()


def test_gemma_loop_applies_delayed_decisions_in_real_time():
    class FakeBrain:
        replies = itertools.cycle(['{"command": "walk"}', "banana"])

        def decide(self, prompt):
            assert "torso tilt" in prompt
            time.sleep(0.3)
            command, parsed = m.parse_command(next(self.replies))
            return m.Decision(command, "x", 0.3, parsed)

        def close(self):
            pass

    cfg = m.GemmaRunConfig(model=Path("brak.litertlm"), max_steps=180, decision_every=30, headless=True)
    started = time.perf_counter()
    stats = m.run_gemma(cfg, brain=FakeBrain())
    elapsed = time.perf_counter() - started
    assert stats["steps"] == 180 and elapsed >= 2.5  # 180 klatek po 1/60 s
    assert stats["decisions"] >= 4 and stats["falls"] == 0  # bez polityki walk działa jak stand
    assert any("nieczytelna" in line for line in cfg.log)


def test_short_training_saves_the_best_model(tmp_path):
    pytest.importorskip("stable_baselines3")
    best = m.train(m.TrainConfig(timesteps=2048, n_envs=1, out_dir=tmp_path, eval_every=1024, eval_episodes=1))
    assert best.exists() and (tmp_path / "vecnormalize_best.pkl").exists()
    returns = m.evaluate(best, episodes=1, headless=True, seed=1)
    assert len(returns) == 1


def test_two_stage_training_continues_from_the_first_stage(tmp_path):
    pytest.importorskip("stable_baselines3")
    stage1 = m.train(m.TrainConfig(timesteps=1024, n_envs=1, out_dir=tmp_path / "s1", eval_every=1024,
                                   eval_episodes=1, reward="basic"))
    stage2 = m.train(m.TrainConfig(timesteps=1024, n_envs=1, out_dir=tmp_path / "s2", eval_every=1024,
                                   eval_episodes=1, reward="humanlike", init=stage1))
    assert stage2.exists() and (tmp_path / "s2" / "vecnormalize_best.pkl").exists()
    with pytest.raises(FileNotFoundError):
        m.train(m.TrainConfig(timesteps=1024, n_envs=1, out_dir=tmp_path / "s3", init=tmp_path / "brak.zip"))
    with pytest.raises(ValueError):
        m.train(m.TrainConfig(timesteps=1024, n_envs=1, out_dir=tmp_path / "s4", reward="sprint"))


def test_reward_presets_differ_only_where_documented():
    basic, humanlike = m.REWARD_PRESETS["basic"], m.REWARD_PRESETS["humanlike"]
    assert humanlike == m.RewardWeights()
    # Etap 1 jak Walker2d-v4: prędkość i przeżycie po 1, koszt sterowania 1e-3·Σa² dla 6 stawów.
    assert (basic.forward, basic.alive, basic.energy) == (1.0, 1.0, pytest.approx(6e-3))
    assert basic.fall == basic.tilt == basic.height == basic.smoothness == 0.0
