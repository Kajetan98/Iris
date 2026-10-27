"""Model ciała HERMES do symulacji (docs/spec.md). Bez I/O, czas podawany z zewnątrz.

Nie importuje niczego z `host`: symulator ma być niezależnym sprawdzianem spec.
Każda metoda przyjmująca `now` oczekuje sekund z zegara monotonicznego.
"""

from __future__ import annotations

import json
import random
import struct
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from sim import audio as sim_audio

PROTOCOL_VERSION = 1
MAX_HOST_FRAME_BYTES = 512  # większe ramki od hosta ciało odrzuca (spec, sekcja 5)
AUDIO_HEADER = struct.Struct(">BHH")  # kind, id, seq (big-endian), sekcja 6
AUDIO_MIC = 1
AUDIO_PLAYBACK = 2

SENSORS = ("mic", "imu", "temp", "presence", "gps")
HAPTIC_PATTERNS = ("attention", "warn", "critical")
MAX_UTTERANCE_S = 60

HELP = """komendy:
  batt <0-100>            ustaw baterię
  drain <%/min>           tempo rozładowania (ujemne = ładowanie)
  temp <C>                ustaw temperaturę
  fault <sensor> [kod]    usterka sensora (mic imu temp presence gps)
  clear <sensor>          koniec usterki
  press [long]            przycisk
  activate                prośba o przełączenie IRIS na to urządzenie (device.activate)
  say [dur_ms]            wypowiedź: ton 440 Hz zakodowany do Opus, ramki z mikrofonu
  sayfile <plik.wav>      wypowiedź z nagrania WAV (PCM 8/16 bitów), zakodowana do Opus
  local <cmd...>          komenda obsłużona lokalnie
  presence on|off         obecność
  gps <lat> <lon>         pozycja
  reboot [powód]          restart ciała (nowy boot_id, ts od zera)
  drop <s>                zerwij połączenie na s sekund
  mute <s>                zostaw połączenie, ale wstrzymaj heartbeaty i odczyty okresowe
  status                  pokaż stan
  quit                    zakończ"""


@dataclass(frozen=True)
class SimConfig:
    batt_pct: float = 71.0
    drain_per_min: float = 0.0
    temp_c: float = 24.0
    rssi: int | None = -58  # None: urządzenie bez pomiaru sygnału
    heartbeat_interval_s: float = 1.0
    sensor_period_s: float = 30.0
    temp_delta: float = 0.5
    buffer_limit: int = 64
    gpio_pins: frozenset[int] = frozenset({5, 6, 7})
    clips: frozenset[str] = frozenset({"ready", "error", "low_battery", "link_lost"})
    audio_rate_hz: int = 16000
    audio_frame_ms: float = 20.0
    audio_bitrate_bps: int = 24000
    seed: int = 1
    fw: str = "sim-0.1"
    # Możliwości urządzenia (device.caps); domyślnie jak HERMES, bufor jak w firmware.
    has: tuple[str, ...] = ("mic", "speaker", "haptic", "gpio", "battery")
    playback_bytes: int = 49152
    playback_packets: int = 1024


@dataclass(frozen=True)
class Out:
    data: str | bytes
    bufferable: bool = False  # tylko zdarzenia; heartbeaty, ack i audio giną przy braku łącza


class BodyModel:
    max_host_frame_bytes = MAX_HOST_FRAME_BYTES  # urządzenie z `display` przyjmuje więcej (spec, sekcja 5)

    def __init__(self, cfg: SimConfig, now: float) -> None:
        self.cfg = cfg
        self._rng = random.Random(cfg.seed)
        self.batt = cfg.batt_pct
        self.drain = cfg.drain_per_min
        self.temp = cfg.temp_c
        self.faults: dict[str, str] = {}
        self.settings: dict[str, float] = {"volume": 50, "batt_warn_pct": 20, "temp_warn_c": 40}
        self.gpio: dict[int, int] = {}
        self.haptic_log: list[str] = []
        self.played: list[tuple[int, int]] = []  # (audio_id, bajty)
        self.played_audio: list[tuple[int, list[bytes]]] = []  # (audio_id, pakiety Opus w kolejności seq)
        self.clips_played: list[str] = []
        self.frame_errors = 0
        self.buffer: deque[Out] = deque()
        self.dropped = 0
        self.offline_until = 0.0
        self.wants_disconnect = False
        self.mute_until = 0.0
        self.quit_requested = False
        self.active = True  # po połączeniu, dopóki host nie przyśle device.active (spec, sekcja 1)
        self._out: list[Out] = []
        self._utt = 0
        self._last_tick = now
        self._next_hb = now
        self._playback: dict[int, list[tuple[int, bytes]]] = {}
        self._actions: dict[str, Callable[[dict[str, Any]], str | None]] = {
            "ping": self._act_ping,
            "haptic": self._act_haptic,
            "speak": self._act_speak,
            "speak.clip": self._act_clip,
            "gpio.set": self._act_gpio,
            "config.set": self._act_config,
            "device.active": self._act_device_active,
        }
        self._boot(now, "power")

    # --- ramki ---

    def _boot(self, now: float, reason: str) -> None:
        self.boot_id = f"{self._rng.getrandbits(32):08x}"
        self.boot_t = now
        self._n = 0
        self.faults.clear()
        self._playback.clear()
        self._sent_temp: float | None = None
        self._last_sensor = now
        self._next_hb = now
        self._emit_event(
            "system.boot",
            {"boot_id": self.boot_id, "fw": self.cfg.fw, "reset_reason": reason},
            now,
        )

    def _frame(self, t: str, type_: str, body: dict[str, Any], now: float) -> str:
        self._n += 1
        frame = {
            "v": PROTOCOL_VERSION,
            "t": t,
            "id": f"{self.boot_id[:4]}.{self._n}",
            "ts": int((now - self.boot_t) * 1000),
            "type": type_,
            "body": body,
        }
        return json.dumps(frame, separators=(",", ":"), ensure_ascii=False)

    def _emit_event(self, type_: str, body: dict[str, Any], now: float, *, bufferable: bool = True) -> None:
        self._out.append(Out(self._frame("event", type_, body, now), bufferable))

    def take_out(self) -> list[Out]:
        out, self._out = self._out, []
        return out

    # --- bufor przy braku łącza ---

    def stash(self, outs: list[Out]) -> None:
        for o in outs:
            if not o.bufferable:
                continue
            if len(self.buffer) >= self.cfg.buffer_limit:
                self.buffer.popleft()
                self.dropped += 1
            self.buffer.append(o)

    def requeue(self, outs: list[Out]) -> None:
        """Wraca na początek bufora (np. po zerwaniu w trakcie opróżniania). Przy przepełnieniu giną najstarsze."""
        self.buffer.extendleft(reversed(outs))
        while len(self.buffer) > self.cfg.buffer_limit:
            self.buffer.popleft()
            self.dropped += 1

    def take_buffer(self) -> list[Out]:
        out = list(self.buffer)
        self.buffer.clear()
        return out

    def on_connect(self, now: float) -> Out:
        """Nowe połączenie: urządzenie aktywne i device.caps do wysłania przed zaległymi zdarzeniami."""
        self.active = True
        caps = {
            "has": list(self.cfg.has),
            "clips": sorted(self.cfg.clips),
            "playback_bytes": self.cfg.playback_bytes,
            "playback_packets": self.cfg.playback_packets,
        }
        return Out(self._frame("event", "device.caps", caps, now))

    # --- upływ czasu ---

    def tick(self, now: float) -> None:
        dt = now - self._last_tick
        self._last_tick = now
        if self.drain:
            self.batt = min(100.0, max(0.0, self.batt - self.drain / 60.0 * dt))
        if now < self.mute_until:
            return
        if now >= self._next_hb:
            self._next_hb = now + self.cfg.heartbeat_interval_s
            hb: dict[str, Any] = {"frame_errors": self.frame_errors}
            if self.cfg.rssi is not None:
                hb = {"link_rssi": self.cfg.rssi, **hb}
            if "battery" in self.cfg.has:
                hb = {"batt_pct": _num(self.batt), **hb}
            self._emit_event("body.heartbeat", hb, now, bufferable=False)
        self._maybe_sensor(now)

    def _maybe_sensor(self, now: float) -> None:
        if "temp" in self.faults:
            return
        temp = round(self.temp, 1)
        due = now - self._last_sensor >= self.cfg.sensor_period_s
        changed = self._sent_temp is None or abs(temp - self._sent_temp) >= self.cfg.temp_delta
        if due or changed:
            self._sent_temp = temp
            self._last_sensor = now
            self._emit_event("body.sensor", {"temp_c": temp}, now)

    # --- komendy z konsoli i skryptu ---

    def command(self, line: str, now: float) -> str | None:
        parts = line.split()
        if not parts:
            return None
        name, args = parts[0].lower(), parts[1:]
        handler = getattr(self, f"_cmd_{name}", None)
        if handler is None:
            return f"nieznana komenda: {name} (help)"
        try:
            return handler(args, now)
        except (ValueError, sim_audio.AudioUnavailable) as e:
            return f"błąd: {e}"

    def _cmd_help(self, args: list[str], now: float) -> str:
        return HELP

    def _cmd_batt(self, args: list[str], now: float) -> str:
        value = _float(args, "batt <0-100>")
        if not 0 <= value <= 100:
            raise ValueError("bateria musi być w zakresie 0-100")
        self.batt = value
        return f"bateria {value:g}%"

    def _cmd_drain(self, args: list[str], now: float) -> str:
        self.drain = _float(args, "drain <%/min>")
        return f"rozładowanie {self.drain:g} %/min"

    def _cmd_temp(self, args: list[str], now: float) -> str:
        self.temp = _float(args, "temp <C>")
        return f"temperatura {self.temp:g} C"

    def _cmd_fault(self, args: list[str], now: float) -> str:
        sensor = _sensor(args, "fault <sensor> [kod]")
        code = args[1] if len(args) > 1 else "fault"
        self.faults[sensor] = code
        self._emit_event("body.fault", {"sensor": sensor, "code": code}, now)
        return f"usterka {sensor}: {code}"

    def _cmd_clear(self, args: list[str], now: float) -> str:
        sensor = _sensor(args, "clear <sensor>")
        if sensor not in self.faults:
            return f"{sensor} nie ma usterki"
        del self.faults[sensor]
        if sensor == "temp":
            self._sent_temp = None
        self._emit_event("body.fault.clear", {"sensor": sensor}, now)
        return f"koniec usterki {sensor}"

    def _cmd_press(self, args: list[str], now: float) -> str:
        kind = "long" if args and args[0] == "long" else "press"
        self._emit_event("button", {"id": "b1", "kind": kind}, now)
        return f"przycisk {kind}"

    def _cmd_activate(self, args: list[str], now: float) -> str:
        # Bez łącza prośba przepada: spóźniona mogłaby przełączyć IRIS nieoczekiwanie.
        self._emit_event("device.activate", {}, now, bufferable=False)
        return "prośba o przełączenie na to urządzenie"

    def _cmd_say(self, args: list[str], now: float) -> str:
        if not self.active:
            return "urządzenie nieaktywne, brak wypowiedzi"
        if "mic" in self.faults:
            return "mikrofon ma usterkę, brak wypowiedzi"
        dur_ms = int(args[0]) if args else 1200
        if not 0 < dur_ms <= MAX_UTTERANCE_S * 1000:
            raise ValueError(f"dur_ms musi być z zakresu 1-{MAX_UTTERANCE_S * 1000}")
        rate = self.cfg.audio_rate_hz
        return self._utterance(sim_audio.tone(440, dur_ms / 1000, rate), rate, dur_ms, now)

    def _cmd_sayfile(self, args: list[str], now: float) -> str:
        if len(args) != 1:
            raise ValueError("sayfile <plik.wav>")
        if not self.active:
            return "urządzenie nieaktywne, brak wypowiedzi"
        if "mic" in self.faults:
            return "mikrofon ma usterkę, brak wypowiedzi"
        samples, rate = sim_audio.read_wav(args[0])
        dur_ms = int(len(samples) * 1000 / rate)
        if not 0 < dur_ms <= MAX_UTTERANCE_S * 1000:
            raise ValueError(f"nagranie musi trwać od 1 ms do {MAX_UTTERANCE_S} s")
        return self._utterance(samples, rate, dur_ms, now)

    def _utterance(self, samples: Any, rate: int, dur_ms: int, now: float) -> str:
        """Zdarzenia start/end i ramki Opus mikrofonu (jeden pakiet na ramkę, spec sekcja 6)."""
        packets = sim_audio.encode_opus(
            samples, rate,
            target_rate=self.cfg.audio_rate_hz,
            frame_ms=self.cfg.audio_frame_ms,
            bitrate_bps=self.cfg.audio_bitrate_bps,
        )
        self._utt += 1
        utt = self._utt
        self._emit_event("audio.utterance.start", {"utt_id": utt}, now)
        for seq, packet in enumerate(packets):
            self._out.append(Out(AUDIO_HEADER.pack(AUDIO_MIC, utt & 0xFFFF, seq) + packet))
        self._emit_event("audio.utterance.end", {"utt_id": utt, "dur_ms": dur_ms}, now)
        return f"wypowiedź {utt}: {len(packets)} pakietów Opus, {dur_ms} ms"

    def _cmd_local(self, args: list[str], now: float) -> str:
        if not args:
            raise ValueError("local <cmd...>")
        cmd = " ".join(args)
        self._emit_event("local.command", {"cmd": cmd, "result": "ok"}, now)
        return f"komenda lokalna: {cmd}"

    def _cmd_presence(self, args: list[str], now: float) -> str:
        if not args or args[0] not in ("on", "off"):
            raise ValueError("presence on|off")
        present = args[0] == "on"
        self._emit_event("body.sensor", {"presence": present}, now)
        return f"obecność {args[0]}"

    def _cmd_gps(self, args: list[str], now: float) -> str:
        if len(args) != 2:
            raise ValueError("gps <lat> <lon>")
        if "gps" in self.faults:
            return "gps ma usterkę, brak pozycji"
        pos = {"lat": float(args[0]), "lon": float(args[1])}
        self._emit_event("body.sensor", {"gps": pos}, now)
        return f"pozycja {pos['lat']:g}, {pos['lon']:g}"

    def _cmd_reboot(self, args: list[str], now: float) -> str:
        reason = args[0] if args else "software"
        self._boot(now, reason)
        return f"restart ({reason}), boot_id {self.boot_id}"

    def _cmd_drop(self, args: list[str], now: float) -> str:
        seconds = _float(args, "drop <s>")
        self.offline_until = now + seconds
        self.wants_disconnect = True
        return f"zrywam połączenie na {seconds:g} s"

    def _cmd_mute(self, args: list[str], now: float) -> str:
        seconds = _float(args, "mute <s>")
        self.mute_until = now + seconds
        return f"cisza na {seconds:g} s"

    def _cmd_quit(self, args: list[str], now: float) -> str:
        self.quit_requested = True
        return "koniec"

    def _cmd_status(self, args: list[str], now: float) -> str:
        return (
            f"boot_id {self.boot_id}, ts {int((now - self.boot_t) * 1000)} ms\n"
            f"bateria {self.batt:.1f}% (rozładowanie {self.drain:g} %/min), temp {self.temp:g} C\n"
            f"usterki: {dict(self.faults) or 'brak'}\n"
            f"bufor {len(self.buffer)}/{self.cfg.buffer_limit}, utracone {self.dropped}, "
            f"odrzucone ramki z hosta {self.frame_errors}\n"
            f"głośność {self.settings['volume']:g}, gpio {self.gpio or '-'}\n"
            f"aktywne urządzenie: {'tak' if self.active else 'nie'}"
        )

    # --- ramki od hosta ---

    def on_host_text(self, raw: str, now: float) -> None:
        """Akcja od hosta. Ramka niezgodna ze spec zwiększa licznik frame_errors."""
        if len(raw.encode("utf-8")) > self.max_host_frame_bytes:
            self.frame_errors += 1
            return
        try:
            data = json.loads(raw)
        except ValueError:
            self.frame_errors += 1
            return
        if (
            not isinstance(data, dict)
            or data.get("v") != PROTOCOL_VERSION
            or data.get("t") != "action"
            or not isinstance(data.get("id"), str)
            or not isinstance(data.get("type"), str)
            or not isinstance(data.get("body", {}), dict)
        ):
            self.frame_errors += 1
            return
        handler = self._actions.get(data["type"])
        if handler is None:
            err: str | None = "unknown_action"
        else:
            try:
                err = handler(data.get("body", {}))
            except (TypeError, ValueError):
                err = "bad_body"
        ack: dict[str, Any] = {"ref": data["id"], "ok": err is None}
        if err is not None:
            ack["err"] = err
        self._out.append(Out(self._frame("ack", data["type"], ack, now)))

    def on_host_binary(self, raw: bytes) -> None:
        if len(raw) < AUDIO_HEADER.size:
            self.frame_errors += 1
            return
        kind, audio_id, seq = AUDIO_HEADER.unpack_from(raw)
        if kind != AUDIO_PLAYBACK:
            self.frame_errors += 1
            return
        if audio_id not in self._playback:
            self._playback.clear()  # jeden bufor: nowe audio_id zastępuje niezapowiedziane ramki
        self._playback.setdefault(audio_id, []).append((seq, bytes(raw[AUDIO_HEADER.size:])))

    # --- akcje: zwracają None (ok) albo tekst błędu ---

    def _act_ping(self, body: dict[str, Any]) -> str | None:
        return None

    def _act_haptic(self, body: dict[str, Any]) -> str | None:
        pattern = body.get("pattern")
        if pattern not in HAPTIC_PATTERNS:
            return "bad_value" if isinstance(pattern, str) else "bad_body"
        self.haptic_log.append(pattern)
        return None

    def _act_speak(self, body: dict[str, Any]) -> str | None:
        audio_id = body.get("audio_id")
        if isinstance(audio_id, bool) or not isinstance(audio_id, (int, str)):
            return "bad_body"
        try:
            key = int(audio_id)
        except ValueError:
            return "bad_body"
        chunks = self._playback.pop(key, None)
        if not chunks:
            return "no_audio"
        self.played.append((key, sum(len(c) for _, c in chunks)))
        self.played_audio.append((key, [c for _, c in sorted(chunks)]))
        return None

    def _act_clip(self, body: dict[str, Any]) -> str | None:
        clip = body.get("clip")
        if clip not in self.cfg.clips:
            return "unknown_clip" if isinstance(clip, str) else "bad_body"
        self.clips_played.append(clip)
        return None

    def _act_gpio(self, body: dict[str, Any]) -> str | None:
        pin, value = body.get("pin"), body.get("value")
        if not _is_int(pin) or not _is_int(value):
            return "bad_body"
        if pin not in self.cfg.gpio_pins:
            return "pin_not_allowed"
        if value not in (0, 1):
            return "bad_value"
        self.gpio[pin] = value
        return None

    def _act_config(self, body: dict[str, Any]) -> str | None:
        key, value = body.get("key"), body.get("value")
        if key not in self.settings:
            return "unknown_key" if isinstance(key, str) else "bad_body"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "bad_body"
        if key in ("volume", "batt_warn_pct") and not 0 <= value <= 100:
            return "bad_value"
        self.settings[key] = value
        return None

    def _act_device_active(self, body: dict[str, Any]) -> str | None:
        active = body.get("active")
        if not isinstance(active, bool):
            return "bad_body"
        self.active = active
        return None


def parse_script(text: str) -> list[tuple[float, str]]:
    """Linie `<sekundy> <komenda>`; `#` zaczyna komentarz. Wynik posortowany po czasie."""
    steps: list[tuple[float, str]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        head, _, command = line.partition(" ")
        try:
            at = float(head)
        except ValueError:
            raise ValueError(f"linia {n}: oczekiwano czasu w sekundach, jest {head!r}") from None
        if at < 0 or not command.strip():
            raise ValueError(f"linia {n}: potrzebny nieujemny czas i komenda")
        steps.append((at, command.strip()))
    steps.sort(key=lambda s: s[0])
    return steps


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _num(x: float) -> float | int:
    x = round(x, 1)
    return int(x) if x.is_integer() else x


def _float(args: list[str], usage: str) -> float:
    if len(args) != 1:
        raise ValueError(usage)
    return float(args[0])


def _sensor(args: list[str], usage: str) -> str:
    if not args:
        raise ValueError(usage)
    if args[0] not in SENSORS:
        raise ValueError(f"nieznany sensor {args[0]!r}, dozwolone: {' '.join(SENSORS)}")
    return args[0]
