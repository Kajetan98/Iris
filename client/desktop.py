"""Program IRIS na komputer i Raspberry Pi: mikrofon i głośnik systemu jako urządzenie (docs/spec.md, sekcja 1).

    python -m client.desktop --url ws://host:8000/ws --device laptop [--vad]

Mówi się z klawiatury: Enter zaczyna wypowiedź, kolejny Enter ją kończy. Z --vad wypowiedź
zaczyna i kończy się sama po głośności; nie ma słowa wybudzającego, więc IRIS odpowiada na
każdą wykrytą mowę. Token hosta ze zmiennej IRIS_TOKEN. Wymaga sounddevice, av i numpy.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from client.audio import (
    DECODE_RATE,
    FRAME,
    RATE,
    AudioBackend,
    AudioUnavailable,
    EnergyVad,
    OpusStream,
    SoundDeviceBackend,
    decode_packets,
)
from sim.body import SimRunner, with_device
from sim.model import AUDIO_HEADER, AUDIO_MIC, HAPTIC_PATTERNS, BodyModel, Out, SimConfig

log = logging.getLogger("client")

MAX_UTTERANCE_S = 60
BATTERY_PERIOD_S = 30.0
HELP = """Enter                   zacznij albo zakończ wypowiedź
activate                przełącz IRIS na to urządzenie
status                  stan urządzenia
quit                    zakończ"""


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def linux_battery() -> float | None:
    """Poziom baterii z /sys (laptop z Linuksem) albo None (komputer stacjonarny, Raspberry Pi)."""
    for supply in Path("/sys/class/power_supply").glob("*"):
        try:
            if (supply / "type").read_text().strip() == "Battery":
                return float((supply / "capacity").read_text().strip())
        except (OSError, ValueError):
            continue
    return None


@dataclass
class _Utterance:
    utt_id: int
    encoder: OpusStream = field(default_factory=OpusStream)
    seq: int = 0
    samples: int = 0


DISPLAY_KINDS = ("heard", "reply", "nothing", "failed")


class DesktopBody(BodyModel):
    """Urządzenie z prawdziwym mikrofonem i głośnikiem. Reguły protokołu jak w symulatorze, ale bez
    zmyślonych odczytów: bez temperatury, RSSI i (bez baterii w systemie) poziomu baterii."""

    max_host_frame_bytes = 4096  # napisy (display)

    def __init__(
        self,
        cfg: SimConfig,
        now: float,
        audio: AudioBackend,
        *,
        battery: Callable[[], float | None] = lambda: None,
        vad: EnergyVad | None = None,
        show: Callable[[str], None] = print,
    ) -> None:
        super().__init__(cfg, now)
        self._actions["display"] = self._act_display
        self._show = show
        self.captions: list[dict[str, Any]] = []
        self.audio = audio
        self.vad = vad
        self._read_battery = battery
        self._next_battery = now
        self.playing = False
        self.busy_drops = 0
        self.talking: _Utterance | None = None
        self.played: list[tuple[int, int]] = []  # (audio_id, próbki 48 kHz)

    # --- czas: bateria z systemu, bez czujników ---

    def tick(self, now: float) -> None:
        if "battery" in self.cfg.has and now >= self._next_battery:
            self._next_battery = now + BATTERY_PERIOD_S
            level = self._read_battery()
            if level is not None:
                self.batt = level
        super().tick(now)

    def _maybe_sensor(self, now: float) -> None:
        return  # komputer nie ma czujników z protokołu; niczego nie wysyłamy

    # --- mikrofon (wołane w pętli zdarzeń) ---

    def start_utterance(self, now: float) -> str:
        if self.talking is not None:
            return "wypowiedź już trwa"
        if not self.active:
            return "urządzenie nieaktywne, brak wypowiedzi"
        self._utt += 1
        self.talking = _Utterance(self._utt)
        self._emit_event("audio.utterance.start", {"utt_id": self._utt}, now)
        return f"mów (wypowiedź {self._utt}), Enter kończy"

    def end_utterance(self, now: float) -> str:
        talk = self.talking
        if talk is None:
            return "brak wypowiedzi"
        self.talking = None
        self._send_packets(talk, talk.encoder.flush())
        dur_ms = int(talk.samples * 1000 / RATE)
        self._emit_event("audio.utterance.end", {"utt_id": talk.utt_id, "dur_ms": dur_ms}, now)
        return f"wypowiedź {talk.utt_id}: {dur_ms} ms"

    def on_mic(self, frame: np.ndarray, now: float) -> None:
        if self.vad is None:
            self._feed(frame, now)
            return
        for kind, data in self.vad.feed(frame):
            if kind == "start" and self.start_utterance(now).startswith("mów"):
                for before in data:
                    self._feed(before, now)
            elif kind == "frame":
                self._feed(data, now)
            elif kind == "end":
                self.end_utterance(now)

    def _feed(self, frame: np.ndarray, now: float) -> None:
        talk = self.talking
        if talk is None:
            return
        talk.samples += FRAME
        self._send_packets(talk, talk.encoder.encode(frame))
        if talk.samples >= MAX_UTTERANCE_S * RATE:
            self.end_utterance(now)

    def _send_packets(self, talk: _Utterance, packets: list[bytes]) -> None:
        for packet in packets:  # audio nie jest buforowane: bez łącza przepada (spec, sekcja 11)
            self._out.append(Out(AUDIO_HEADER.pack(AUDIO_MIC, talk.utt_id & 0xFFFF, talk.seq) + packet))
            talk.seq += 1

    # --- odtwarzanie ---

    def on_host_binary(self, raw: bytes) -> None:
        if self.playing:
            self.busy_drops += 1  # jeden bufor: w trakcie odtwarzania nic nie przyjmujemy
            return
        super().on_host_binary(raw)

    def _act_speak(self, body: dict[str, Any]) -> str | None:
        audio_id = body.get("audio_id")
        if isinstance(audio_id, bool) or not isinstance(audio_id, (int, str)):
            return "bad_body"
        try:
            key = int(audio_id)
        except ValueError:
            return "bad_body"
        if self.playing:
            return "busy"
        chunks = self._playback.pop(key, None)
        if not chunks:
            return "no_audio"
        by_seq = dict(chunks)
        packets = [by_seq.get(seq) for seq in range(max(by_seq) + 1)]
        pcm = decode_packets(packets)
        self.playing = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # bez pętli (np. testy synchroniczne): odtwarzanie od razu
            self.audio.play(pcm, DECODE_RATE)
            self._playback_done(key, pcm.size)
            return None
        task = loop.run_in_executor(None, self.audio.play, pcm, DECODE_RATE)
        task.add_done_callback(lambda _: self._playback_done(key, pcm.size))
        return None

    def _playback_done(self, audio_id: int, samples: int) -> None:
        self.playing = False
        self.played.append((audio_id, samples))

    # --- napisy w terminalu (spec, sekcja 6) ---

    def _act_display(self, body: dict[str, Any]) -> str | None:
        kind, text = body.get("kind"), body.get("text")
        if kind not in DISPLAY_KINDS:
            return "bad_value" if isinstance(kind, str) else "bad_body"
        if (kind in ("heard", "reply")) != isinstance(text, str):
            return "bad_body"
        self.captions.append(body)
        if kind == "heard":
            self._show(f"Rozpoznano: {text}")
        elif kind == "reply":
            self._show(f"IRIS: {text}" + (" (odpowiedź skrócona)" if body.get("truncated") else ""))
        elif kind == "nothing":
            self._show("Nic nie rozpoznano.")
        else:
            self._show("Nie udało się przygotować odpowiedzi.")
        return None

    # --- akcje bez sprzętu ---

    def _act_haptic(self, body: dict[str, Any]) -> str | None:
        pattern = body.get("pattern")
        if pattern not in HAPTIC_PATTERNS:
            return "bad_value" if isinstance(pattern, str) else "bad_body"
        return "unsupported"

    def _act_gpio(self, body: dict[str, Any]) -> str | None:
        if not _is_int(body.get("pin")) or not _is_int(body.get("value")):
            return "bad_body"
        return "unsupported"

    # --- komendy ---

    def command(self, line: str, now: float) -> str | None:
        name = line.strip().lower()
        if name == "":
            return self.end_utterance(now) if self.talking else self.start_utterance(now)
        if name == "help":
            return HELP
        if name in ("activate", "quit"):
            return super().command(name, now)
        if name == "status":
            batt = f"{self.batt:.0f}%" if "battery" in self.cfg.has else "brak"
            return (
                f"boot_id {self.boot_id}, aktywne: {'tak' if self.active else 'nie'}, "
                f"bateria {batt}, wypowiedź: {'trwa' if self.talking else 'nie'}, "
                f"odtwarzanie: {'tak' if self.playing else 'nie'}, bufor {len(self.buffer)}"
            )
        return f"nieznana komenda: {name} (help)"


def desktop_config(device: str, battery: float | None, **overrides: Any) -> SimConfig:
    has = ("mic", "speaker", "display", "battery") if battery is not None else ("mic", "speaker", "display")
    return SimConfig(
        batt_pct=battery or 0.0, rssi=None, has=has, clips=frozenset(), gpio_pins=frozenset(),
        fw=f"desktop-0.1 ({device})", playback_bytes=1024 * 1024, playback_packets=4096, **overrides,
    )


class DesktopRunner(SimRunner):
    """Połączenie jak w symulatorze (ponawianie, bufor, caps); mikrofon podłączony do pętli zdarzeń."""

    def __init__(self, body: DesktopBody, url: str, **kwargs: Any) -> None:
        super().__init__(body.cfg, url, model=body, **kwargs)
        self.body = body

    async def run(self) -> None:
        loop = asyncio.get_running_loop()

        def on_frame(frame: np.ndarray) -> None:  # wątek audio -> pętla zdarzeń
            loop.call_soon_threadsafe(self.body.on_mic, frame, self.clock())

        self.body.audio.open_input(on_frame)
        try:
            await super().run()
        finally:
            self.body.audio.close_input()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m client.desktop", description=__doc__.split("\n")[0])
    p.add_argument("--url", default="ws://localhost:8000/ws")
    p.add_argument("--device", default="komputer", help="identyfikator urządzenia (a-z, 0-9, -)")
    p.add_argument("--vad", action="store_true", help="wypowiedzi wykrywane po głośności, bez Enter")
    p.add_argument("--vad-threshold", type=float, default=-40.0, help="próg głośności, dBFS")
    p.add_argument("--vad-end-ms", type=int, default=800, help="cisza kończąca wypowiedź, ms")
    p.add_argument("--input-device", help="mikrofon (nazwa lub numer z --list-devices)")
    p.add_argument("--output-device", help="głośnik (nazwa lub numer z --list-devices)")
    p.add_argument("--list-devices", action="store_true", help="wypisz urządzenia audio i zakończ")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def _device(value: str | None) -> str | int | None:
    return int(value) if value is not None and value.isdigit() else value


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(name)s %(message)s")
    try:
        audio = SoundDeviceBackend(_device(args.input_device), _device(args.output_device))
    except AudioUnavailable as e:
        print(e, file=sys.stderr)
        return 2
    if args.list_devices:
        print(audio._sd.query_devices())  # noqa: SLF001
        return 0
    battery = linux_battery()
    vad = EnergyVad(threshold_dbfs=args.vad_threshold, end_ms=args.vad_end_ms) if args.vad else None
    body = DesktopBody(desktop_config(args.device, battery), time.monotonic(), audio, battery=linux_battery, vad=vad)
    runner = DesktopRunner(
        body, with_device(args.url, args.device), console=True, token=os.environ.get("IRIS_TOKEN") or None
    )
    print(HELP if not args.vad else "nasłuch po głośności (--vad); quit kończy", file=sys.stderr)
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
