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
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from host.api.auth import token_from_env
from host.api.rest import router, ws_router
from host.api.service import HostService
from host.core.clock import Clock, SystemClock
from host.core.config import Config
from host.store import Store

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"  # klient w przeglądarce (spec, sekcja 1)


def _preload(*engines: Any) -> None:
    """Ładuje modele przy starcie, żeby błąd wyszedł od razu, a nie przy pierwszej wypowiedzi."""
    for engine in engines:
        load = getattr(engine, "load", None)
        if load is not None:
            load()


async def _close_voice(stt: Any, tts: Any, owned: bool) -> None:
    if not owned:
        return
    for engine in (stt, tts):
        if engine is not None:
            await engine.aclose()


def load_config() -> Config:
    path = os.environ.get("IRIS_CONFIG")
    return Config.load(path) if path else Config()


_FROM_ENV = object()


def create_app(
    config: Config | None = None,
    clock: Clock | None = None,
    store: Store | None = None,
    assistant: Any = None,
    stt: Any = None,
    tts: Any = None,
    *,
    run_tick_loop: bool = True,
    token: Any = _FROM_ENV,
) -> FastAPI:
    """token: None wyłącza uwierzytelnianie; domyślnie IRIS_TOKEN (spec, sekcja 1)."""
    if token is _FROM_ENV:
        token = token_from_env()
    config = config or load_config()  # CORS potrzebuje konfiguracji już przy budowie aplikacji

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if token is None:
            log.warning("brak IRIS_TOKEN: API i /ws bez uwierzytelniania, tylko dla połączeń z tego komputera")
        cfg = config
        clk = clock or SystemClock()
        owned_store = store is None
        st = store or Store(cfg.store.path, clk)
        owned_assistant = assistant is None
        asst = assistant
        if asst is None and cfg.brain.llm.engine != "none":
            from host.brain import build_assistant  # host.brain tylko przy włączonym LLM

            asst = build_assistant(cfg.brain.llm)
        stt_engine, tts_engine, voice_status = stt, tts, "disabled"
        owned_voice = stt is None and tts is None
        if owned_voice and cfg.brain.stt.engine != "none":
            from host.brain import build_stt, build_tts  # głos tylko przy włączonym STT i TTS

            stt_engine, tts_engine = build_stt(cfg.brain.stt), build_tts(cfg.brain.tts)
        if stt_engine is not None and tts_engine is not None:
            try:
                await asyncio.to_thread(_preload, stt_engine, tts_engine)
            except Exception as e:  # brak modelu nie może zatrzymać reszty hosta
                log.error("głos wyłączony, nie udało się załadować modeli: %s", str(e)[:300])
                await _close_voice(stt_engine, tts_engine, owned_voice)
                stt_engine = tts_engine = None
                voice_status = "error"
        service = HostService(cfg, clk, st, assistant=asst, stt=stt_engine, tts=tts_engine)
        if voice_status == "error":
            service.voice_status = voice_status
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
            await service.drain_voice()
            service.stop()
            await _close_voice(stt_engine, tts_engine, owned_voice)
            if asst is not None and owned_assistant:
                await asst.aclose()
            if owned_store:
                st.close()

    app = FastAPI(title="IRIS host", lifespan=lifespan)
    app.state.token = token
    app.state.allowed_origins = tuple(config.api.cors_origins)  # Origin dopuszczony bez tokenu (spec, sekcja 1)
    if config.api.cors_origins:
        # Klient z innego adresu (np. Vercel); token nadal obowiązuje, ciasteczek nie ma (spec, sekcja 10).
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.api.cors_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
        )
    app.include_router(router)
    app.include_router(ws_router)
    # Klient w przeglądarce jest publiczny: nie zawiera danych, token podaje użytkownik.
    app.mount("/app", StaticFiles(directory=STATIC, html=True), name="app")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/app/")

    return app


app = create_app()
