"""Robot uczący się chodzić: PyBullet + Gymnasium + PPO (Stable-Baselines3) i "mózg" Gemma przez LiteRT-LM.

INSTALACJA (Python 3.10-3.13, najlepiej w osobnym środowisku wirtualnym):

    python -m venv venv
    venv\\Scripts\\activate            (Windows)   |   source venv/bin/activate   (Linux, macOS)
    python -m pip install --upgrade pip
    pip install "gymnasium>=1.0" "numpy>=1.24" pybullet
    pip install torch                  (wersja CPU, mniejsza: dodaj --index-url https://download.pytorch.org/whl/cpu)
    pip install "stable-baselines3>=2.3"
    pip install litert-lm-api          (tylko dla trybu "gemma")

    PyBullet nie ma gotowych pakietów dla każdej kombinacji systemu i Pythona. Wtedy pip kompiluje go
    ze źródeł (kilka minut), co wymaga kompilatora C++:
      Linux:   sudo apt install build-essential python3-dev
      Windows: Microsoft C++ Build Tools (składnik "Programowanie aplikacji klasycznych w C++")
      macOS:   xcode-select --install

UŻYCIE:

    python main.py train --timesteps 2000000      trening PPO bez okna, najlepszy model w models/best_model.zip
    python main.py eval                           pokaz wyuczonego robota w oknie 3D (tryb deterministyczny)
    python main.py gemma --model gemma-4-E2B-it.litertlm --policy models/best_model.zip

ŚRODOWISKO. Gymnasium nie ma robota kroczącego na PyBullet: "Ant-v4" działa na MuJoCo, a "BipedalWalker-v3"
na Box2D (2D). Ten plik rejestruje więc własne środowisko Gymnasium "HumanlikeWalker-v0" na PyBullet,
z dwunożnym robotem Walker2D dostarczanym z PyBullet (mjcf/walker2d.xml): tułów, dwie nogi, 6 stawów
(biodro, kolano, kostka). Ruch odbywa się w płaszczyźnie pionowej, więc robot przewraca się tylko do
przodu albo do tyłu.

GEMMA JAKO MÓZG. Model językowy odpowiada w czasie rzędu sekund, a stabilny chód wymaga decyzji
60 razy na sekundę. Gemma działa więc jako warstwa decyzyjna: co kilkadziesiąt klatek dostaje opis
stanu robota i wybiera zachowanie (iść albo stać). Momenty w stawach liczy kontroler niskiego
poziomu: polityka PPO dla chodu i regulator PD dla postawy. Zapytanie do modelu idzie w osobnym wątku,
więc symulacja nie zatrzymuje się w trakcie odpowiedzi.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import gymnasium as gym
import numpy as np
import pybullet as p
import pybullet_data
from gymnasium import spaces

ENV_ID = "HumanlikeWalker-v0"
MAX_EPISODE_STEPS = 1000  # 1000 kroków sterowania po 1/60 s = około 16,7 s symulacji
DEFAULT_MODEL_DIR = Path("models")


class SimulatorClosed(RuntimeError):
    """Okno PyBullet zostało zamknięte albo połączenie z serwerem fizyki zerwane."""


# =============================================================================
# 1. ŚRODOWISKO: robot Walker2D w PyBullet
# =============================================================================


class WalkerBulletEnv(gym.Env):
    """Dwunożny robot w PyBullet ze sterowaniem momentem w 6 stawach.

    Akcja: 6 liczb z [-1, 1], ułamek maksymalnego momentu w stawie (prawe biodro, kolano, kostka,
    potem lewe). Kierunki stawów w tym modelu: ujemny kąt biodra odchyla nogę do tyłu, ujemny kąt
    kolana zgina je jak u człowieka.

    Obserwacja (20 liczb): wysokość tułowia względem startu, sin i cos przechyłu tułowia, prędkość
    pozioma i pionowa, prędkość kątowa tułowia, 6 kątów stawów w skali [-1, 1], 6 prędkości stawów
    i kontakt każdej stopy z podłożem (0 albo 1).
    """

    metadata = {"render_modes": ["human"], "render_fps": 60}

    ROOT_JOINTS = ("ignorex", "ignorez", "ignorey")  # pasywne złącza: przesunięcie x, wysokość, przechył
    MOTOR_JOINTS = (
        "thigh_joint", "leg_joint", "foot_joint",
        "thigh_left_joint", "leg_left_joint", "foot_left_joint",
    )
    FEET = ("foot", "foot_left")
    MAX_TORQUE = 40.0  # N·m; w pliku MJCF gear=100, a środowiska PyBullet używają mocy 0,4 tej wartości
    TIME_STEP = 1.0 / 240.0  # krok fizyki
    FRAME_SKIP = 4  # krok sterowania = 4 kroki fizyki = 1/60 s
    STANDING_HEIGHT = 1.25  # wysokość środka tułowia na starcie (m)
    FALL_HEIGHT = 0.8  # niżej robot leży
    FALL_PITCH = 1.0  # rad (około 57°) przechyłu tułowia to upadek

    def __init__(self, render_mode: str | None = None) -> None:
        super().__init__()
        if render_mode not in (None, *self.metadata["render_modes"]):
            raise ValueError(f"nieobsługiwany render_mode: {render_mode}")
        self.render_mode = render_mode
        self.dt = self.TIME_STEP * self.FRAME_SKIP
        self.action_space = spaces.Box(-1.0, 1.0, shape=(len(self.MOTOR_JOINTS),), dtype=np.float32)
        self.observation_space = spaces.Box(-np.inf, np.inf, shape=(20,), dtype=np.float32)

        mode = p.GUI if render_mode == "human" else p.DIRECT
        self._client = p.connect(mode)
        if self._client < 0:
            raise RuntimeError("nie udało się połączyć z PyBullet (czy działa środowisko graficzne?)")
        if render_mode == "human":
            p.configureDebugVisualizer(p.COV_ENABLE_GUI, 0, physicsClientId=self._client)
        p.setAdditionalSearchPath(pybullet_data.getDataPath(), physicsClientId=self._client)
        self._robot = -1
        self._plane = -1
        self._root: list[int] = []
        self._motors: list[int] = []
        self._feet: list[int] = []
        self._torso = -1
        self._lower = np.zeros(len(self.MOTOR_JOINTS))
        self._upper = np.zeros(len(self.MOTOR_JOINTS))
        self._last_frame = 0.0
        self._previous_position = (0.0, 0.0)  # (x, wysokość tułowia) po poprzednim kroku

    # --- połączenie ---

    @property
    def connected(self) -> bool:
        return self._client >= 0 and p.isConnected(physicsClientId=self._client)

    def _require_connection(self) -> None:
        if not self.connected:
            raise SimulatorClosed("okno PyBullet zostało zamknięte")

    # --- API Gymnasium ---

    def reset(self, *, seed: int | None = None, options: dict[str, Any] | None = None):
        super().reset(seed=seed)
        self._require_connection()
        cid = self._client
        try:
            p.resetSimulation(physicsClientId=cid)
            p.setGravity(0, 0, -9.81, physicsClientId=cid)
            p.setTimeStep(self.TIME_STEP, physicsClientId=cid)
            self._plane = p.loadURDF("plane.urdf", physicsClientId=cid)
            self._robot = p.loadMJCF("mjcf/walker2d.xml", physicsClientId=cid)[0]
            self._index_joints()
            # Domyślne silniki prędkości PyBullet trzymają stawy w miejscu: wyłączamy je, żeby działał
            # tylko moment z akcji, a złącza tułowia były pasywne.
            all_joints = list(range(p.getNumJoints(self._robot, physicsClientId=cid)))
            p.setJointMotorControlArray(
                self._robot, all_joints, p.VELOCITY_CONTROL, forces=[0.0] * len(all_joints), physicsClientId=cid
            )
            # Mały losowy szum postawy startowej: polityka nie uczy się jednej trajektorii na pamięć.
            for j in self._motors:
                p.resetJointState(self._robot, j, self.np_random.uniform(-0.05, 0.05), 0.0, physicsClientId=cid)
            for j in self._root:
                p.resetJointState(self._robot, j, 0.0, 0.0, physicsClientId=cid)
        except p.error as e:
            raise SimulatorClosed(str(e)) from e
        self._last_frame = time.perf_counter()
        state = self._read_state()
        self._previous_position = (state["x"], state["height"])
        state["vx_mean"], state["vz_mean"] = 0.0, 0.0
        return self._observation(state), self._info(state, np.zeros(len(self._motors)))

    def step(self, action):
        self._require_connection()
        action = np.clip(np.asarray(action, dtype=np.float64), -1.0, 1.0)
        cid = self._client
        try:
            p.setJointMotorControlArray(
                self._robot, self._motors, p.TORQUE_CONTROL,
                forces=(action * self.MAX_TORQUE).tolist(), physicsClientId=cid,
            )
            for _ in range(self.FRAME_SKIP):
                p.stepSimulation(physicsClientId=cid)
            state = self._read_state()
        except p.error as e:
            raise SimulatorClosed(str(e)) from e
        # Prędkości do nagrody liczone z przesunięcia w całym kroku sterowania, nie z odczytu chwilowego:
        # chwilowa prędkość z końca kroku pozwala "zarabiać" drganiem w miejscu, które w chwili odczytu
        # zawsze idzie do przodu.
        x_before, height_before = self._previous_position
        state["vx_mean"] = (state["x"] - x_before) / self.dt
        state["vz_mean"] = (state["height"] - height_before) / self.dt
        self._previous_position = (state["x"], state["height"])
        fallen = state["height"] < self.FALL_HEIGHT or abs(state["pitch"]) > self.FALL_PITCH
        # Nagroda bazowa: postęp i przeżycie. Właściwą nagrodę "ludzkiego" chodu liczy HumanlikeGaitReward.
        reward = float(state["vx_mean"]) + (0.0 if fallen else 1.0)
        if self.render_mode == "human":
            self._follow_camera(state)
            self._sleep_to_real_time()
        return self._observation(state), reward, bool(fallen), False, self._info(state, action)

    def close(self) -> None:
        if self.connected:
            try:
                p.disconnect(physicsClientId=self._client)
            except p.error:
                pass  # okno zamknięte w tej samej chwili
        self._client = -1

    # --- pomocnicze ---

    def _index_joints(self) -> None:
        cid = self._client
        names: dict[str, int] = {}
        links: dict[str, int] = {}
        for j in range(p.getNumJoints(self._robot, physicsClientId=cid)):
            info = p.getJointInfo(self._robot, j, physicsClientId=cid)
            names[info[1].decode()] = j
            links[info[12].decode()] = j  # indeks ogniwa jest równy indeksowi złącza, które je prowadzi
        self._root = [names[n] for n in self.ROOT_JOINTS]
        self._motors = [names[n] for n in self.MOTOR_JOINTS]
        self._feet = [links[n] for n in self.FEET]
        self._torso = links["torso"]
        limits = [p.getJointInfo(self._robot, j, physicsClientId=cid)[8:10] for j in self._motors]
        self._lower = np.array([lo for lo, _ in limits])
        self._upper = np.array([hi for _, hi in limits])

    def _read_state(self) -> dict[str, Any]:
        cid = self._client
        root = p.getJointStates(self._robot, self._root, physicsClientId=cid)
        joints = p.getJointStates(self._robot, self._motors, physicsClientId=cid)
        torso = p.getLinkState(self._robot, self._torso, computeForwardKinematics=True, physicsClientId=cid)
        contacts = [
            1.0 if p.getContactPoints(self._robot, self._plane, linkIndexA=f, physicsClientId=cid) else 0.0
            for f in self._feet
        ]
        return {
            "x": root[0][0],
            "vx": root[0][1],
            "vz": root[1][1],
            "pitch": root[2][0],  # dodatni = pochylenie do przodu
            "pitch_rate": root[2][1],
            "height": torso[0][2],
            "q": np.array([s[0] for s in joints]),
            "dq": np.array([s[1] for s in joints]),
            "contacts": np.array(contacts),
        }

    def _observation(self, s: dict[str, Any]) -> np.ndarray:
        mid = (self._upper + self._lower) / 2
        half = np.maximum((self._upper - self._lower) / 2, 1e-6)
        obs = np.concatenate(
            [
                [s["height"] - self.STANDING_HEIGHT, math.sin(s["pitch"]), math.cos(s["pitch"])],
                [s["vx"], s["vz"], s["pitch_rate"]],
                (s["q"] - mid) / half,
                0.1 * s["dq"],
                s["contacts"],
            ]
        )
        return obs.astype(np.float32)

    def _info(self, s: dict[str, Any], action: np.ndarray) -> dict[str, Any]:
        return {
            "x_position": float(s["x"]),
            "forward_velocity": float(s["vx_mean"]),  # średnia w kroku sterowania (m/s)
            "vertical_velocity": float(s["vz_mean"]),  # średnia zmiana wysokości tułowia (m/s)
            "pitch": float(s["pitch"]),
            "pitch_rate": float(s["pitch_rate"]),
            "torso_height": float(s["height"]),
            "joint_positions": s["q"].copy(),
            "joint_velocities": s["dq"].copy(),
            "feet_contacts": s["contacts"].copy(),
            "action": np.asarray(action, dtype=np.float64).copy(),
            "fallen": bool(s["height"] < self.FALL_HEIGHT or abs(s["pitch"]) > self.FALL_PITCH),
        }

    def _follow_camera(self, s: dict[str, Any]) -> None:
        p.resetDebugVisualizerCamera(
            cameraDistance=3.0, cameraYaw=0.0, cameraPitch=-15.0,
            cameraTargetPosition=[s["x"], 0.0, 1.0], physicsClientId=self._client,
        )

    def _sleep_to_real_time(self) -> None:
        delay = self.dt - (time.perf_counter() - self._last_frame)
        if delay > 0:
            time.sleep(delay)
        self._last_frame = time.perf_counter()


def register_env() -> None:
    """Rejestruje środowisko w Gymnasium. Wołane też w procesach potomnych (SubprocVecEnv), bo przy
    uruchamianiu "spawn" i "forkserver" nie muszą one wykonać kodu modułu na nowo."""
    if ENV_ID not in gym.registry:
        gym.register(id=ENV_ID, entry_point=WalkerBulletEnv, max_episode_steps=MAX_EPISODE_STEPS)


register_env()


# =============================================================================
# 2. FUNKCJA NAGRODY "LUDZKIEGO" CHODU
# =============================================================================


@dataclass
class RewardWeights:
    forward: float = 1.5  # w_v: nagroda za prędkość do przodu
    target_speed: float = 1.2  # v_max (m/s): powyżej tej prędkości nagroda nie rośnie
    alive: float = 1.0  # w_alive: premia za każdy krok bez upadku
    tilt: float = 2.0  # w_θ: kara za przechył tułowia
    tilt_rate: float = 0.1  # w_ω: kara za kołysanie tułowia (prędkość kątowa)
    bob: float = 0.5  # w_z: kara za podskakiwanie (prędkość pionowa)
    height: float = 3.0  # w_h: kara za odejście od wysokości stania (środek ciężkości)
    energy: float = 0.1  # w_e: kara za kwadrat momentu
    power: float = 0.02  # w_p: kara za moc mechaniczną |τ·ω|
    smoothness: float = 0.3  # w_s: kara za szarpnięcia (zmiana akcji między krokami)
    airborne: float = 0.5  # w_air: kara za lot bez kontaktu stóp (człowiek idąc nie podskakuje)
    fall: float = 50.0  # P_fall: jednorazowa kara za upadek
    reference_height: float = 1.2  # h_ref (m): wysokość tułowia przy naturalnym chodzie


# Presety wag dla dwóch etapów treningu (--reward). Od razu z pełnymi karami polityka uczy się stać:
# 1000 punktów za przeżycie jest pewne, a pierwszy krok grozi upadkiem. Etap "basic" ma proporcje
# nagrody Walker2d-v4 z Gymnasium (MuJoCo): prędkość 1, przeżycie 1, koszt sterowania 1e-3·Σa²
# (= 0,006·średnia z 6 stawów), bez innych kar. Etap "humanlike" doucza chód karami za przechył,
# podskoki, energię i szarpanie (--init z modelu po etapie "basic").
REWARD_PRESETS: dict[str, RewardWeights] = {
    "basic": RewardWeights(
        forward=1.0, target_speed=10.0, tilt=0.0, tilt_rate=0.0, bob=0.0, height=0.0,
        energy=0.006, power=0.0, smoothness=0.0, airborne=0.0, fall=0.0,
    ),
    "humanlike": RewardWeights(),
}


class HumanlikeGaitReward(gym.Wrapper):
    """Zastępuje nagrodę środowiska nagrodą za stabilny, oszczędny i płynny chód.

    Dla kroku t, akcji a_t (n = 6 stawów, wartości w [-1, 1]) i stanu tułowia:

        r_t =  w_v · min(v_x, v_max)                      postęp do przodu, bez premii za bieg
             + w_alive                                     przeżycie kroku
             - w_θ · θ²  -  w_ω · θ̇²                      tułów pionowo, bez kiwania
             - w_z · v_z²  -  w_h · (h - h_ref)²           stały środek ciężkości, bez podskoków
             - w_e · (1/n) Σ a_i²                          energia: moment² jest proporcjonalny do strat
                                                           cieplnych w silniku (I²R)
             - w_p · (1/n) Σ |a_i · q̇_i|                   moc mechaniczna przekazana do stawów
             - w_s · (1/n) Σ (a_i,t - a_i,t-1)²            szarpanie: duża zmiana momentu w 1/60 s
             - w_air · [żadna stopa nie dotyka podłoża]
             - P_fall · [upadek]

    Kwadraty w karach rosną szybciej niż liniowo: drobne odchylenia kosztują mało, duże bardzo
    dużo, więc polityka dąży do spokojnego ruchu wokół pozycji równowagi. Ograniczenie
    min(v_x, v_max) usuwa motywację do chaotycznego biegu. Upadek kończy epizod (terminated=True),
    a algorytm od razu zaczyna nowy.

    v_x i v_z to średnie prędkości w kroku sterowania (przesunięcie tułowia podzielone przez 1/60 s).
    Prędkość chwilowa z końca kroku pozwalała zdobywać nagrodę za postęp drganiem w miejscu.
    """

    def __init__(self, env: gym.Env, weights: RewardWeights | None = None) -> None:
        super().__init__(env)
        self.weights = weights or RewardWeights()
        self._previous_action = np.zeros(env.action_space.shape, dtype=np.float64)

    def reset(self, **kwargs):
        obs, info = self.env.reset(**kwargs)
        self._previous_action = np.zeros_like(self._previous_action)
        return obs, info

    def step(self, action):
        obs, _, terminated, truncated, info = self.env.step(action)
        reward, terms = self.compute(info, self._previous_action)
        self._previous_action = np.asarray(info["action"], dtype=np.float64)
        info["reward_terms"] = terms
        return obs, reward, terminated, truncated, info

    def compute(self, info: dict[str, Any], previous_action: np.ndarray) -> tuple[float, dict[str, float]]:
        w = self.weights
        a = np.asarray(info["action"], dtype=np.float64)
        dq = np.asarray(info["joint_velocities"], dtype=np.float64)
        fallen = bool(info["fallen"])
        terms = {
            "forward": w.forward * min(info["forward_velocity"], w.target_speed),
            "alive": 0.0 if fallen else w.alive,
            "tilt": -w.tilt * info["pitch"] ** 2,
            "tilt_rate": -w.tilt_rate * info["pitch_rate"] ** 2,
            "bob": -w.bob * info["vertical_velocity"] ** 2,
            "height": -w.height * (info["torso_height"] - w.reference_height) ** 2,
            "energy": -w.energy * float(np.mean(a**2)),
            "power": -w.power * float(np.mean(np.abs(a * dq))),
            "smoothness": -w.smoothness * float(np.mean((a - previous_action) ** 2)),
            "airborne": -w.airborne if not np.any(info["feet_contacts"]) else 0.0,
            "fall": -w.fall if fallen else 0.0,
        }
        return float(sum(terms.values())), terms


# =============================================================================
# 3. TRENING PPO
# =============================================================================


@dataclass
class TrainConfig:
    timesteps: int = 2_000_000
    n_envs: int = 8
    seed: int = 0
    out_dir: Path = DEFAULT_MODEL_DIR
    eval_every: int = 20_000  # kroków łącznie (wszystkie środowiska)
    eval_episodes: int = 5
    render: bool = False
    torch_threads: int = 1  # wątki PyTorch w procesie uczącym
    reward: str = "humanlike"  # klucz REWARD_PRESETS
    init: Path | None = None  # model do douczenia (statystyki normalizacji obok, jak w stats_for)


def ppo_hyperparameters(initial_lr: float = 3e-4) -> dict[str, Any]:
    """Ustawienia PPO dobrane pod stabilność nauki chodu (ciągłe sterowanie momentem).

    - n_steps · n_envs = 512 · 8 = 4096 próbek na aktualizację: gradient z wielu epizodów naraz ma
      mniejszą wariancję, więc polityka nie "zapomina" chodu po jednej złej serii.
    - batch_size 128 dzieli 4096 bez reszty; n_epochs 10 to umiarkowane wykorzystanie próbek.
    - learning_rate maleje liniowo od 3e-4 do 0: duże kroki na początku, drobne poprawki na końcu.
    - clip_range 0.2 ogranicza zmianę polityki w jednej aktualizacji (główny mechanizm stabilności PPO).
    - gamma 0.99 i gae_lambda 0.95: horyzont kilku sekund, wystarczający dla cyklu kroku.
    - use_sde (gSDE) losuje szum eksploracji raz na kilka kroków zamiast w każdym kroku, więc ruchy
      podczas nauki są gładsze i mniej szarpane; log_std_init -2 to mały szum startowy.
    - max_grad_norm 0.5 przycina gradient przy nagłych skokach nagrody (np. upadek).
    """
    import torch.nn as nn

    return {
        "learning_rate": lambda progress_remaining: initial_lr * progress_remaining,
        "n_steps": 512,
        "batch_size": 128,
        "n_epochs": 10,
        "gamma": 0.99,
        "gae_lambda": 0.95,
        "clip_range": 0.2,
        "ent_coef": 0.0,
        "vf_coef": 0.5,
        "max_grad_norm": 0.5,
        "use_sde": True,
        "sde_sample_freq": 4,
        "policy_kwargs": {
            "log_std_init": -2.0,
            "ortho_init": False,
            "activation_fn": nn.ReLU,
            "net_arch": {"pi": [256, 256], "vf": [256, 256]},
        },
    }


def make_env(rank: int, seed: int, render_mode: str | None = None, weights: RewardWeights | None = None):
    """Fabryka środowiska dla wektora środowisk SB3 (każde w osobnym procesie lub w tym samym)."""

    def _init() -> gym.Env:
        from stable_baselines3.common.monitor import Monitor

        register_env()
        env = gym.make(ENV_ID, render_mode=render_mode)
        env = HumanlikeGaitReward(env, replace(weights) if weights is not None else None)
        env = Monitor(env)  # zapisuje sumę nagrody i długość epizodu
        env.reset(seed=seed + rank)
        return env

    return _init


def train(cfg: TrainConfig) -> Path:
    from stable_baselines3 import PPO
    from stable_baselines3.common.callbacks import BaseCallback, EvalCallback
    from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv, VecNormalize

    if cfg.reward not in REWARD_PRESETS:
        raise ValueError(f"nieznany preset nagrody: {cfg.reward} (dostępne: {', '.join(REWARD_PRESETS)})")
    weights = REWARD_PRESETS[cfg.reward]
    if cfg.init is not None:
        for path in (cfg.init, stats_for(cfg.init)):
            if not path.exists():
                raise FileNotFoundError(f"Brak {path} (model do douczenia i jego statystyki normalizacji).")
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    stats_best = cfg.out_dir / "vecnormalize_best.pkl"

    class SaveNormalizationOnBest(BaseCallback):
        """Przy nowym najlepszym modelu zapisuje też statystyki normalizacji obserwacji."""

        def _on_step(self) -> bool:
            vec_norm = self.model.get_vec_normalize_env()
            if vec_norm is not None:
                vec_norm.save(str(stats_best))
            return True

    # Trening bez okna (p.DIRECT) w kilku procesach: GUI PyBullet działa tylko w jednym procesie.
    render_mode = "human" if cfg.render else None
    n_envs = 1 if cfg.render else cfg.n_envs
    env_fns = [make_env(i, cfg.seed, render_mode, weights) for i in range(n_envs)]
    raw_venv = SubprocVecEnv(env_fns) if n_envs > 1 else DummyVecEnv(env_fns)
    if cfg.init is None:
        # Normalizacja obserwacji i nagrody: sieć dostaje wartości rzędu 1, co stabilizuje gradienty.
        venv = VecNormalize(raw_venv, norm_obs=True, norm_reward=True, clip_obs=10.0, gamma=0.99)
    else:
        # Douczanie: polityka zna obserwacje tylko w skali z poprzedniego treningu.
        venv = VecNormalize.load(str(stats_for(cfg.init)), raw_venv)
        venv.training, venv.norm_reward = True, True

    eval_env = VecNormalize(
        DummyVecEnv([make_env(10_000, cfg.seed, None, weights)]), norm_obs=True, norm_reward=False, training=False
    )
    eval_callback = EvalCallback(
        eval_env,
        best_model_save_path=str(cfg.out_dir),  # zapisuje best_model.zip
        log_path=str(cfg.out_dir / "eval"),
        eval_freq=max(cfg.eval_every // n_envs, 1),
        n_eval_episodes=cfg.eval_episodes,
        deterministic=True,
        callback_on_new_best=SaveNormalizationOnBest(),
        verbose=1,
    )
    # Sieci 256x256 są małe: przy wielu wątkach PyTorch synchronizacja kosztuje więcej, niż daje liczenie
    # równoległe, a wątki konkurują o rdzenie z procesami symulacji. Pomiar na 4 rdzeniach, 4 środowiska:
    # 1 wątek 567 kroków/s, 2 wątki 516, 4 wątki 420.
    import torch

    torch.set_num_threads(cfg.torch_threads)
    if cfg.init is None:
        model = PPO("MlpPolicy", venv, seed=cfg.seed, verbose=1, device="cpu", **ppo_hyperparameters())
    else:
        # Mniejszy współczynnik uczenia przy douczaniu: duże kroki na starcie mogą zgubić wyuczony chód.
        lr = ppo_hyperparameters(initial_lr=1e-4)["learning_rate"]
        model = PPO.load(str(cfg.init), env=venv, device="cpu", custom_objects={"learning_rate": lr})
        model.set_random_seed(cfg.seed)
    try:
        model.learn(total_timesteps=cfg.timesteps, callback=eval_callback, progress_bar=False)
    except KeyboardInterrupt:
        print("\nTrening przerwany (Ctrl+C), zapisuję stan.")
    except SimulatorClosed:
        print("\nOkno PyBullet zamknięte, zapisuję stan.")
    finally:
        model.save(str(cfg.out_dir / "final_model"))
        venv.save(str(cfg.out_dir / "vecnormalize_final.pkl"))
        venv.close()
        eval_env.close()
    best = cfg.out_dir / "best_model.zip"
    if not best.exists():  # ocena nie zdążyła się odbyć (bardzo krótki trening)
        model.save(str(best.with_suffix("")))
        venv_stats = cfg.out_dir / "vecnormalize_final.pkl"
        stats_best.write_bytes(venv_stats.read_bytes())
    print(f"Najlepszy model: {best}")
    return best


# =============================================================================
# 4. PĘTLA TESTOWA (wyuczony robot, tryb deterministyczny)
# =============================================================================


def load_policy(model_path: Path, stats_path: Path | None, render_mode: str | None, seed: int):
    """Wczytuje politykę PPO i środowisko ze statystykami normalizacji z treningu."""
    from stable_baselines3 import PPO
    from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

    venv = DummyVecEnv([make_env(0, seed, render_mode)])
    if stats_path is not None and stats_path.exists():
        venv = VecNormalize.load(str(stats_path), venv)
        venv.training = False  # statystyki zamrożone
        venv.norm_reward = False  # pokazujemy prawdziwą nagrodę
    else:
        print(f"Uwaga: brak {stats_path}, obserwacje bez normalizacji (polityka może działać źle).")
    model = PPO.load(str(model_path), device="cpu")
    return model, venv


def stats_for(model_path: Path) -> Path:
    name = "vecnormalize_best.pkl" if model_path.stem == "best_model" else "vecnormalize_final.pkl"
    return model_path.parent / name


def evaluate(model_path: Path, episodes: int, headless: bool, seed: int) -> list[float]:
    model, venv = load_policy(model_path, stats_for(model_path), None if headless else "human", seed)
    returns: list[float] = []
    try:
        for episode in range(episodes):
            obs = venv.reset()
            total, steps, done = 0.0, 0, False
            while not done:
                action, _ = model.predict(obs, deterministic=True)
                obs, reward, dones, infos = venv.step(action)
                total += float(reward[0])
                steps += 1
                done = bool(dones[0])
            # DummyVecEnv resetuje środowisko po końcu epizodu; ostatni stan jest w terminal_observation.
            distance = infos[0].get("x_position", float("nan"))
            fell = infos[0].get("fallen", False)
            print(f"Epizod {episode + 1}: nagroda {total:.1f}, kroki {steps}, dystans {distance:.2f} m"
                  f"{', upadek' if fell else ''}")
            returns.append(total)
    except SimulatorClosed:
        print("Okno PyBullet zamknięte, koniec pokazu.")
    finally:
        venv.close()
    return returns


# =============================================================================
# 5. GEMMA (LiteRT-LM) JAKO WARSTWA DECYZYJNA
# =============================================================================

COMMANDS = ("walk", "stand")

SYSTEM_PROMPT = (
    "You control a two-legged walking robot in a physics simulation. "
    "Every half second you receive its sensor readings and choose one behavior:\n"
    "- walk: walk forward with the learned gait\n"
    "- stand: stop and hold an upright standing posture until the robot is steady\n"
    "Goal: walk forward as far as possible without falling. Choose stand when the torso tilts "
    "more than about 20 degrees, tilts fast, or the robot is much lower than standing height; "
    "choose walk when it is steady. Reply with JSON only, for example {\"command\": \"walk\"}."
)

COMMAND_SCHEMA = {
    "type": "object",
    "properties": {"command": {"type": "string", "enum": list(COMMANDS)}},
    "required": ["command"],
}


@dataclass
class Decision:
    command: str
    raw: str
    latency_s: float
    parsed: bool  # False: odpowiedź nieczytelna, użyto komendy bezpiecznej


def state_to_prompt(info: dict[str, Any]) -> str:
    """Zamienia stan fizyczny robota na krótki opis tekstowy dla modelu.

    Prompt jest po angielsku: mały model (2 mld parametrów) trzyma się formatu JSON pewniej w tym
    języku. Liczby są zaokrąglone, a obok każdej stoi jej znaczenie, bo model nie zna jednostek symulatora.
    """
    pitch = info["pitch"]
    tilt = "forward" if pitch > 0.05 else "backward" if pitch < -0.05 else "upright"
    contacts = info["feet_contacts"]
    feet = {2: "both feet on the ground", 1: "one foot on the ground", 0: "no foot on the ground"}[
        int(np.sum(contacts))
    ]
    return (
        "Robot state:\n"
        f"- torso height: {info['torso_height']:.2f} m (standing is about 1.25 m, below 0.8 m means fallen)\n"
        f"- torso tilt: {math.degrees(pitch):+.0f} degrees ({tilt}; beyond ±57 degrees means fallen)\n"
        f"- tilt rate: {math.degrees(info['pitch_rate']):+.0f} degrees per second\n"
        f"- forward speed: {info['forward_velocity']:+.2f} m/s\n"
        f"- {feet}\n"
        f"- distance walked: {info['x_position']:.1f} m\n"
        "Choose the next behavior."
    )


def parse_command(text: str) -> tuple[str, bool]:
    """Wyciąga komendę z odpowiedzi modelu: najpierw JSON, potem samo słowo. Inaczej "stand"."""
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if match:
        try:
            command = str(json.loads(match.group(0)).get("command", "")).strip().lower()
            if command in COMMANDS:
                return command, True
        except (json.JSONDecodeError, AttributeError):
            pass
    lowered = text.lower()
    for command in COMMANDS:
        if command in lowered or command.replace("_", " ") in lowered:
            return command, True
    return "stand", False  # postawa jest najbezpieczniejsza, gdy model odpowie nie na temat


class GemmaBrain:
    """Gemma uruchomiona lokalnie przez LiteRT-LM (pakiet litert-lm-api, moduł litert_lm).

    Każda decyzja to nowa rozmowa: kontekst nie rośnie, a odpowiedź zależy tylko od bieżącego stanu.
    Jeśli wersja biblioteki lub model nie obsługuje dekodowania ograniczonego schematem JSON,
    zapytanie idzie bez niego, a komendę wyciąga parse_command.
    """

    def __init__(self, model_path: Path, max_output_tokens: int = 32) -> None:
        import litert_lm

        if not model_path.is_file():
            raise FileNotFoundError(f"brak pliku modelu: {model_path}")
        self._lm = litert_lm
        self._max_output_tokens = max_output_tokens
        self._constrained = True
        print(f"Wczytuję {model_path.name} przez LiteRT-LM (CPU), to może potrwać kilkanaście sekund...")
        self._engine = litert_lm.Engine(str(model_path), backend=litert_lm.Backend.CPU())

    def decide(self, prompt: str) -> Decision:
        started = time.perf_counter()
        text = self._ask(prompt)
        command, parsed = parse_command(text)
        return Decision(command, text, time.perf_counter() - started, parsed)

    def _ask(self, prompt: str) -> str:
        lm = self._lm
        if self._constrained:
            try:
                with self._engine.create_conversation(
                    system_message=SYSTEM_PROMPT,
                    constrained_decoding_config=lm.ConstrainedDecodingConfig(enable=True),
                    max_output_tokens=self._max_output_tokens,
                ) as conversation:
                    reply = conversation.send_message(
                        prompt, response_format=lm.ResponseFormat.json(COMMAND_SCHEMA)
                    )
                    return str(reply)
            except Exception as e:  # np. model bez obsługi schematów: dalej bez ograniczeń
                print(f"Dekodowanie ze schematem JSON niedostępne ({type(e).__name__}), pytam bez niego.")
                self._constrained = False
        with self._engine.create_conversation(
            system_message=SYSTEM_PROMPT, max_output_tokens=self._max_output_tokens
        ) as conversation:
            return str(conversation.send_message(prompt))

    def close(self) -> None:
        self._engine.close()


class AsyncDecider:
    """Zapytania do modelu w osobnym wątku: symulacja biegnie dalej z poprzednią komendą,
    a nowa obowiązuje od klatki, w której przyszła odpowiedź."""

    def __init__(self, brain: Any) -> None:
        self._brain = brain
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gemma")
        self._pending: concurrent.futures.Future | None = None

    @property
    def busy(self) -> bool:
        return self._pending is not None and not self._pending.done()

    def request(self, prompt: str) -> bool:
        """Wysyła zapytanie, jeśli poprzednie już się skończyło. Zwraca False, gdy model jeszcze myśli."""
        if self.busy:
            return False
        self._pending = self._pool.submit(self._brain.decide, prompt)
        return True

    def poll(self) -> Decision | None:
        """Gotowa decyzja albo None. Błąd modelu zamienia na komendę "stand"."""
        if self._pending is None or not self._pending.done():
            return None
        future, self._pending = self._pending, None
        try:
            return future.result()
        except Exception as e:
            return Decision("stand", f"błąd modelu: {type(e).__name__}: {e}", 0.0, False)

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)


# Postawa stojąca: nogi prawie proste. Biodro i kolano mają w tym modelu ogranicznik w 0 rad, więc
# wyprostowany staw przenosi ciężar na ogranicznik, a silnik tylko koryguje.
STAND_POSE = np.array([0.0, -0.05, 0.0, 0.0, -0.05, 0.0])


@dataclass
class PostureController:
    """Regulator PD postawy z korektą równowagi, w ułamku maksymalnego momentu:

        a_i = Kp · (q*_i - q_i) - Kd · q̇_i  +  k_θ,i · θ  +  k_ω,i · θ̇

    θ i θ̇ to przechył tułowia i jego prędkość. Główną pracę robią kostki (k_θ = -4,87): robot stoi jak
    odwrócone wahadło, a moment w kostkach przesuwa środek nacisku stóp pod środek ciężkości.
    Wzmocnienia dobrano przeszukaniem losowym w tej symulacji: robot stoi 30 s na 10 losowych startach,
    ale silne popchnięcie (0,3-0,6 rad/s) zwykle go przewraca.
    """

    kp: float = 12.87
    kd: float = 0.33
    hip_gains: tuple[float, float] = (0.21, 0.04)  # (k_θ, k_ω) dla bioder
    knee_gains: tuple[float, float] = (0.14, 0.01)
    ankle_gains: tuple[float, float] = (-4.87, -1.5)

    def action(self, info: dict[str, Any]) -> np.ndarray:
        q = np.asarray(info["joint_positions"])
        dq = np.asarray(info["joint_velocities"])
        a = self.kp * (STAND_POSE - q) - self.kd * dq
        theta, omega = info["pitch"], info["pitch_rate"]
        for joints, (k_theta, k_omega) in (
            ((0, 3), self.hip_gains), ((1, 4), self.knee_gains), ((2, 5), self.ankle_gains)
        ):
            a[list(joints)] += k_theta * theta + k_omega * omega
        return np.clip(a, -1.0, 1.0)


@dataclass
class GemmaRunConfig:
    model: Path
    policy: Path | None = None
    decision_every: int = 30  # klatek sterowania między zapytaniami (30 × 1/60 s = 0,5 s)
    max_steps: int = 3000
    headless: bool = False
    seed: int = 0
    log: list[str] = field(default_factory=list)


def run_gemma(cfg: GemmaRunConfig, brain: Any | None = None) -> dict[str, Any]:
    """Pętla sterowania z modelem językowym.

    Częstotliwości:
      - fizyka 240 Hz, sterowanie 60 Hz (akcja w każdej klatce), zawsze w czasie rzeczywistym,
      - decyzja Gemmy co `decision_every` klatek, tylko jeśli poprzednia odpowiedź już przyszła;
        przy odpowiedzi trwającej 2 s komenda zmienia się więc mniej więcej co 2 s,
      - między decyzjami kontroler niskiego poziomu wykonuje ostatnią komendę.
    """
    policy, venv = None, None
    if cfg.policy is not None:
        policy, venv = load_policy(cfg.policy, stats_for(cfg.policy), None if cfg.headless else "human", cfg.seed)
        # Pętla steruje pojedynczym środowiskiem bezpośrednio: komenda zmienia źródło akcji w każdej klatce.
        env = venv.venv.envs[0] if hasattr(venv, "venv") else venv.envs[0]
    else:
        print("Bez --policy komenda walk działa jak stand: chód wymaga wytrenowanej polityki PPO.")
        env = HumanlikeGaitReward(gym.make(ENV_ID, render_mode=None if cfg.headless else "human"))
    posture = PostureController()
    own_brain = brain is None
    brain = brain or GemmaBrain(cfg.model)
    decider = AsyncDecider(brain)
    command = "stand"
    stats = {"decisions": 0, "falls": 0, "steps": 0, "distance": 0.0, "commands": {c: 0 for c in COMMANDS}}

    def log(message: str) -> None:
        cfg.log.append(message)
        print(message)

    def policy_input(obs: np.ndarray) -> np.ndarray:
        # Polityka widziała w treningu obserwacje znormalizowane statystykami VecNormalize.
        batch = obs[None]
        return venv.normalize_obs(batch) if hasattr(venv, "normalize_obs") else batch

    frame = WalkerBulletEnv.TIME_STEP * WalkerBulletEnv.FRAME_SKIP
    try:
        obs, info = env.reset(seed=cfg.seed)
        next_frame = time.perf_counter()
        for step in range(cfg.max_steps):
            # Opóźnienie modelu liczy się w prawdziwych sekundach, więc symulacja też biegnie w czasie
            # rzeczywistym (z oknem tempo trzyma środowisko, bez okna ta pętla).
            if cfg.headless:
                next_frame += frame
                delay = next_frame - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
            if step % cfg.decision_every == 0:
                decider.request(state_to_prompt(info))
            decision = decider.poll()
            if decision is not None:
                stats["decisions"] += 1
                stats["commands"][decision.command] += 1
                note = "" if decision.parsed else " (odpowiedź nieczytelna, komenda bezpieczna)"
                log(f"[krok {step:5d}] Gemma po {decision.latency_s:.1f} s: {decision.command}{note}  "
                    f"<- {decision.raw.strip()[:80]!r}")
                command = decision.command

            if command == "walk" and policy is not None:
                action, _ = policy.predict(policy_input(obs), deterministic=True)
                action = action[0]
            else:
                action = posture.action(info)

            obs, _, terminated, truncated, info = env.step(action)
            stats["steps"] = step + 1
            stats["distance"] = max(stats["distance"], info["x_position"])
            if terminated or truncated:
                if terminated:
                    stats["falls"] += 1
                    log(f"[krok {step:5d}] upadek po {info['x_position']:.2f} m, reset")
                obs, info = env.reset()
                command = "stand"
    except SimulatorClosed:
        log("Okno PyBullet zamknięte, koniec.")
    except KeyboardInterrupt:
        log("Przerwano (Ctrl+C).")
    finally:
        decider.close()
        if own_brain:
            brain.close()
        if venv is not None:
            venv.close()
        else:
            env.close()
    return stats


# =============================================================================
# WIERSZ POLECEŃ
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Robot uczący się chodzić (PyBullet, PPO, Gemma).")
    sub = parser.add_subparsers(dest="mode", required=True)

    t = sub.add_parser("train", help="trening PPO")
    t.add_argument("--timesteps", type=int, default=2_000_000)
    t.add_argument("--n-envs", type=int, default=min(8, os.cpu_count() or 1))
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--out", type=Path, default=DEFAULT_MODEL_DIR)
    t.add_argument("--eval-every", type=int, default=20_000)
    t.add_argument("--render", action="store_true", help="trening z oknem 3D (jedno środowisko, wolno)")
    t.add_argument("--reward", choices=sorted(REWARD_PRESETS), default="humanlike",
                   help="wagi nagrody: basic (etap 1, nauka chodu) albo humanlike (etap 2, styl chodu)")
    t.add_argument("--init", type=Path, default=None, help="douczanie zapisanego modelu, np. models/best_model.zip")

    e = sub.add_parser("eval", help="pokaz wyuczonej polityki")
    e.add_argument("--policy", type=Path, default=DEFAULT_MODEL_DIR / "best_model.zip")
    e.add_argument("--episodes", type=int, default=3)
    e.add_argument("--headless", action="store_true", help="bez okna 3D")
    e.add_argument("--seed", type=int, default=123)

    g = sub.add_parser("gemma", help="sterowanie decyzjami modelu Gemma przez LiteRT-LM")
    g.add_argument("--model", type=Path, default=Path("gemma-4-E2B-it.litertlm"))
    g.add_argument("--policy", type=Path, default=None, help="polityka PPO dla komendy walk")
    g.add_argument("--decision-every", type=int, default=30, help="klatki między decyzjami (60 klatek = 1 s)")
    g.add_argument("--max-steps", type=int, default=3000)
    g.add_argument("--headless", action="store_true")
    g.add_argument("--seed", type=int, default=0)

    args = parser.parse_args(argv)
    try:
        if args.mode == "train":
            train(TrainConfig(args.timesteps, args.n_envs, args.seed, args.out, args.eval_every,
                              render=args.render, reward=args.reward, init=args.init))
        elif args.mode == "eval":
            if not args.policy.exists():
                print(f"Brak {args.policy}. Najpierw: python main.py train", file=sys.stderr)
                return 1
            evaluate(args.policy, args.episodes, args.headless, args.seed)
        else:
            if args.policy is not None and not args.policy.exists():
                print(f"Brak {args.policy}. Najpierw: python main.py train", file=sys.stderr)
                return 1
            stats = run_gemma(GemmaRunConfig(args.model, args.policy, args.decision_every, args.max_steps,
                                             args.headless, args.seed))
            print(f"Podsumowanie: {stats}")
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    except ImportError as e:
        print(f"Brak biblioteki: {e.name}. Instalacja: komentarz na początku pliku.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # wymagane przy wielu procesach (SubprocVecEnv) na Windows i macOS
    sys.exit(main())
