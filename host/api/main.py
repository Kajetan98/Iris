"""Aplikacja FastAPI. Uruchomienie: `uvicorn host.api.main:app`.

Konfiguracja z pliku TOML wskazanego zmienną IRIS_CONFIG (bez niej: wartości domyślne).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from host.api.rest import router
from host.api.service import HostService
from host.core.clock import Clock, SystemClock
from host.core.config import Config
from host.store import Store

log = logging.getLogger(__name__)


def load_config() -> Config:
    path = os.environ.get("IRIS_CONFIG")
    return Config.load(path) if path else Config()


def create_app(
    config: Config | None = None,
    clock: Clock | None = None,
    store: Store | None = None,
    *,
    run_tick_loop: bool = True,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = config or load_config()
        clk = clock or SystemClock()
        owned_store = store is None
        st = store or Store(cfg.store.path, clk)
        service = HostService(cfg, clk, st)
        service.start()
        app.state.service = service
        tick_task = asyncio.create_task(service.run_tick_loop()) if run_tick_loop else None
        try:
            yield
        finally:
            if tick_task is not None:
                tick_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tick_task
            service.stop()
            if owned_store:
                st.close()

    app = FastAPI(title="IRIS host", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
