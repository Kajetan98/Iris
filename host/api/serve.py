"""Host na dwóch portach z jednym stanem: zwykły (HERMES, `ws://`) i z TLS (przeglądarki, `wss://`).

    python -m host.api.serve --host 0.0.0.0 --port 8000 --tls-port 8443 --certfile cert.pem --keyfile key.pem

Przeglądarka daje mikrofon tylko na `localhost` albo przez HTTPS, a firmware HERMES nie ma TLS
(docs/spec.md, sekcje 1 i 13). Konfiguracja i token jak przy `uvicorn host.api.main:app`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Iterator

import uvicorn

from host.api.auth import AuthConfigError, is_loopback_host, token_from_env

log = logging.getLogger("host.api.serve")


class _Server(uvicorn.Server):
    """Sygnały obsługuje main() dla obu serwerów naraz."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m host.api.serve", description=__doc__.split("\n")[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000, help="port bez TLS")
    p.add_argument("--tls-port", type=int, help="port z TLS (wymaga --certfile i --keyfile)")
    p.add_argument("--certfile")
    p.add_argument("--keyfile")
    p.add_argument("--log-level", default="info")
    args = p.parse_args(argv)
    if args.tls_port is not None and not (args.certfile and args.keyfile):
        p.error("--tls-port wymaga --certfile i --keyfile")
    return args


async def serve(args: argparse.Namespace) -> None:
    from host.api.main import create_app  # import tworzy aplikację i czyta IRIS_TOKEN

    app = create_app()
    plain = _Server(uvicorn.Config(app, host=args.host, port=args.port, log_level=args.log_level))
    servers = [plain]
    tasks = [asyncio.create_task(plain.serve())]
    # Drugi serwer bez lifespan: stan (HostService) uruchamia tylko pierwszy.
    while not plain.started and not tasks[0].done():
        await asyncio.sleep(0.05)
    if args.tls_port is not None and plain.started:
        tls = _Server(
            uvicorn.Config(
                app, host=args.host, port=args.tls_port, lifespan="off", log_level=args.log_level,
                ssl_certfile=args.certfile, ssl_keyfile=args.keyfile,
            )
        )
        servers.append(tls)
        tasks.append(asyncio.create_task(tls.serve()))

    def stop() -> None:
        for server in reversed(servers):  # najpierw TLS, potem serwer ze stanem
            server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, stop)
    try:
        for task in reversed(tasks):
            await task
    finally:
        stop()
        await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        token = token_from_env()
    except AuthConfigError as e:
        print(e, file=sys.stderr)
        return 2
    if token is None and not is_loopback_host(args.host):
        # Bez tokenu host odrzuca połączenia z innych komputerów, więc taki start byłby pomyłką (spec, sekcja 10).
        print(f"--host {args.host} wymaga IRIS_TOKEN; bez tokenu host działa tylko na 127.0.0.1", file=sys.stderr)
        return 2
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(name)s %(message)s")
    try:
        asyncio.run(serve(args))
    except AuthConfigError as e:
        print(e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
