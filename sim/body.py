"""Symulator ciała HERMES: klient WebSocket dla hosta IRIS.

    python -m sim.body --url ws://localhost:8000/ws [--device laptop]

Token hosta (IRIS_TOKEN) czytany ze zmiennej środowiskowej i wysyłany jako podprotokół (spec, sekcja 1).

Komendy z konsoli (stdin) i ze skryptu (`--script`, linie `<sekundy> <komenda>`) opisuje `help`.
Przy braku łącza symulator buforuje zdarzenia (limit `--buffer-limit`) i łączy się ponownie.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.typing import Subprotocol

from sim.model import BodyModel, Out, SimConfig, parse_script

log = logging.getLogger("sim")

TICK_S = 0.05
BACKOFF_START_S = 0.5
BACKOFF_MAX_S = 5.0


class SimRunner:
    def __init__(
        self,
        cfg: SimConfig,
        url: str,
        *,
        script: list[tuple[float, str]] | None = None,
        console: bool = False,
        clock: Callable[[], float] = time.monotonic,
        say: Callable[[str], None] = print,
        token: str | None = None,
    ) -> None:
        self.url = url
        self._subprotocols = [Subprotocol("iris"), Subprotocol(f"token.{token}")] if token else None
        self.clock = clock
        self.say = say
        self.model = BodyModel(cfg, clock())
        self.connections = 0
        self._script = list(script or [])
        self._console = console
        self._start = clock()
        self._ws: ClientConnection | None = None
        self._stop: asyncio.Event | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    def command(self, line: str) -> None:
        """Wykonuje komendę tak jak z konsoli i wypisuje odpowiedź."""
        reply = self.model.command(line, self.clock())
        if reply:
            self.say(reply)
        if self.model.quit_requested:
            self.stop()

    async def run(self) -> None:
        self._stop = asyncio.Event()
        tasks = [
            asyncio.create_task(self._ticker(), name="sim-ticker"),
            asyncio.create_task(self._connection_loop(), name="sim-connection"),
        ]
        if self._console:
            tasks.append(asyncio.create_task(self._console_loop(), name="sim-console"))
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._ws is not None:
                await self._ws.close()

    # --- czas i wysyłka ---

    async def _ticker(self) -> None:
        while True:
            now = self.clock()
            while self._script and self._script[0][0] <= now - self._start:
                self.command(self._script.pop(0)[1])
            self.model.tick(now)
            await self._send(self.model.take_out())
            if self.model.wants_disconnect:
                self.model.wants_disconnect = False
                if self._ws is not None:
                    await self._ws.close()
            await asyncio.sleep(TICK_S)

    async def _send(self, outs: list[Out]) -> None:
        for i, out in enumerate(outs):
            ws = self._ws
            if ws is None:
                self.model.stash(outs[i:])
                return
            try:
                await ws.send(out.data)
            except ConnectionClosed:
                self._ws = None
                self.model.stash(outs[i:])
                return

    # --- połączenie ---

    async def _connection_loop(self) -> None:
        backoff = BACKOFF_START_S
        while True:
            while self.clock() < self.model.offline_until:
                await asyncio.sleep(TICK_S)
            try:
                async with connect(self.url, ping_interval=None, subprotocols=self._subprotocols) as ws:
                    backoff = BACKOFF_START_S
                    await self._attach(ws)
                    log.info("połączono z %s", self.url)
                    try:
                        async for message in ws:
                            now = self.clock()
                            if isinstance(message, str):
                                self.model.on_host_text(message, now)
                            else:
                                self.model.on_host_binary(message)
                            await self._send(self.model.take_out())
                    finally:
                        self._ws = None
            except (OSError, WebSocketException) as e:
                log.info("brak połączenia (%s), ponawiam za %.1f s", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)
            else:
                # także po zastąpieniu połączenia (kod 4000), spec sekcja 1
                log.info("połączenie zamknięte, ponawiam za %.1f s", backoff)
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)

    async def _attach(self, ws: ClientConnection) -> None:
        """Najpierw zbuforowane zdarzenia, potem bieżący ruch. Bez await między ostatnim
        odczytem bufora a ustawieniem _ws, więc nic nie wyprzedzi zaległych zdarzeń."""
        await ws.send(self.model.on_connect(self.clock()).data)
        while True:
            pending = self.model.take_buffer()
            if not pending:
                break
            for i, out in enumerate(pending):
                try:
                    await ws.send(out.data)
                except ConnectionClosed:
                    self.model.requeue(pending[i:])
                    raise
        self._ws = ws
        self.connections += 1

    # --- konsola ---

    async def _console_loop(self) -> None:
        loop = asyncio.get_running_loop()
        lines: asyncio.Queue[str | None] = asyncio.Queue()

        def reader() -> None:
            for line in sys.stdin:
                loop.call_soon_threadsafe(lines.put_nowait, line)
            loop.call_soon_threadsafe(lines.put_nowait, None)

        threading.Thread(target=reader, daemon=True).start()
        while (line := await lines.get()) is not None:
            self.command(line)


def with_device(url: str, device: str) -> str:
    """Dodaje parametr device do adresu /ws (spec, sekcja 1)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "device"] + [("device", device)]
    return urlunsplit(parts._replace(query=urlencode(query)))


def build_config(args: argparse.Namespace) -> SimConfig:
    return SimConfig(
        batt_pct=args.batt,
        drain_per_min=args.drain_per_min,
        temp_c=args.temp,
        rssi=args.rssi,
        heartbeat_interval_s=args.heartbeat_interval,
        sensor_period_s=args.sensor_period,
        buffer_limit=args.buffer_limit,
        gpio_pins=frozenset(int(p) for p in args.gpio_pins.split(",") if p),
        seed=args.seed,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m sim.body", description=__doc__.split("\n")[0])
    p.add_argument("--url", default="ws://localhost:8000/ws")
    p.add_argument("--device", help="identyfikator urządzenia (domyślnie hermes)")
    p.add_argument("--batt", type=float, default=71.0, help="początkowa bateria, %%")
    p.add_argument("--drain-per-min", type=float, default=0.0, help="rozładowanie, %%/min (ujemne = ładowanie)")
    p.add_argument("--temp", type=float, default=24.0, help="temperatura, C")
    p.add_argument("--rssi", type=int, default=-58)
    p.add_argument("--heartbeat-interval", type=float, default=1.0, help="sekundy")
    p.add_argument("--sensor-period", type=float, default=30.0, help="sekundy między odczytami okresowymi")
    p.add_argument("--buffer-limit", type=int, default=64, help="zdarzenia buforowane przy braku łącza")
    p.add_argument("--gpio-pins", default="5,6,7", help="biała lista pinów, po przecinku")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--script", help="plik ze scenariuszem: linie '<sekundy> <komenda>'")
    p.add_argument("--no-console", action="store_true", help="nie czytaj komend ze stdin")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(name)s %(message)s")
    script = None
    if args.script:
        try:
            with open(args.script, encoding="utf-8") as f:
                script = parse_script(f.read())
        except (OSError, ValueError) as e:
            print(f"skrypt: {e}", file=sys.stderr)
            return 2
    url = with_device(args.url, args.device) if args.device else args.url
    runner = SimRunner(
        build_config(args), url, script=script, console=not args.no_console, token=os.environ.get("IRIS_TOKEN") or None
    )
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
