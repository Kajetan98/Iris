"""Pełny stos: sim -> host -> POST /chat -> atrapa Ollamy (wszystko na prawdziwych gniazdach)."""

import asyncio

import httpx
import pytest
from host.api.main import create_app
from host.brain.learn import EXTRACT_PROMPT
from host.core.config import Config
from host.tests.servers import free_port, ollama_stub_app, start, stop, until
from sim.body import SimRunner
from sim.model import SimConfig


@pytest.fixture
def ollama_stub():
    app, state = ollama_stub_app()
    port = free_port()
    server, thread = start(app, port)
    yield port, state
    stop(server, thread)


@pytest.fixture
def host(ollama_stub):
    ollama_port, _ = ollama_stub
    cfg = Config.model_validate(
        {
            "store": {"path": ":memory:"},
            "link": {"timeout_s": 0.8, "recover_s": 0.4},
            "drives": {
                "battery_low": {
                    "direction": "below", "enter": 20, "exit": 25,
                    "enter_hold_s": 0.3, "exit_hold_s": 0.3,
                }
            },
            "api": {"tick_interval_s": 0.1},
            "brain": {
                "llm": {
                    "engine": "ollama", "model": "bielik", "retries": 0, "timeout_s": 3,
                    "base_url": f"http://127.0.0.1:{ollama_port}",
                }
            },
        }
    )
    port = free_port()
    server, thread = start(create_app(cfg), port)
    yield port
    stop(server, thread)


def chat_requests(ollama):
    """Zapytania rozmowy; zapytania o wnioski (spec, sekcja 9) idą w tle i mogą przyjść później."""
    return [r for r in ollama["requests"] if r["messages"][0]["content"] != EXTRACT_PROMPT]


def context_of(request_body):
    return request_body["messages"][-1]["content"].split("[WYPOWIEDŹ UŻYTKOWNIKA]")[0]


def without_time(context):
    """Linia z czasem zmienia się między zapytaniami; reszta kontekstu ma być taka sama."""
    return "\n".join(line for line in context.splitlines() if not line.startswith("Czas:"))


def test_chat_through_the_whole_stack(host, ollama_stub):
    _, ollama = ollama_stub

    async def scenario():
        sim = SimRunner(
            SimConfig(heartbeat_interval_s=0.1, sensor_period_s=0.5),
            f"ws://127.0.0.1:{host}/ws",
            say=lambda s: None,
        )
        task = asyncio.create_task(sim.run())
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{host}", timeout=10) as http:

            async def drives():
                return (await http.get("/self")).json()["self"]["drives"]

            await until(lambda: _link_ok(http))
            sim.command("batt 10")
            sim.command("gps 52.2297 21.0122")
            await until(lambda: _has(drives, "battery_low"))

            # 1. udana tura: stan trafia do modelu, współrzędne nie
            r = await http.post("/chat", json={"text": "Jak z baterią?"})
            assert r.status_code == 200
            assert r.json()["text"] == "Bateria jest niska."  # markdown usunięty
            assert r.json()["usage"] == {"input_tokens": 120, "output_tokens": 6}
            first = chat_requests(ollama)[-1]
            assert first["stream"] is False and first["model"] == "bielik"
            assert first["messages"][0]["role"] == "system" and "jesteś programem" in first["messages"][0]["content"].lower()
            ctx = context_of(first)
            assert "oszczędzanie energii" in ctx and "niski poziom baterii ciała" in ctx
            assert "bateria: brak danych → 10%" in ctx
            assert "52.2297" not in str(first) and "21.0122" not in str(first)

            # 2. awaria silnika: 503, a zmiana stanu nie ginie
            sim.command("batt 50")
            await until(lambda: _lacks(drives, "battery_low"))
            ollama["fail"] = True
            r = await http.post("/chat", json={"text": "A teraz?"})
            assert r.status_code == 503
            failed = chat_requests(ollama)[-1]
            ollama["fail"] = False

            # 3. ponowienie dostaje te same zmiany stanu (cofnięcie wycinka)
            r = await http.post("/chat", json={"text": "A teraz?"})
            assert r.status_code == 200
            retry = chat_requests(ollama)[-1]
            assert "bateria: 10% → 50%" in context_of(retry)
            assert without_time(context_of(failed)) == without_time(context_of(retry))

            # 4. historia: nieudana tura nie wchodzi, udane tak
            assert [(m["role"], m["content"]) for m in retry["messages"][1:3]] == [
                ("user", "Jak z baterią?"),
                ("assistant", "Bateria jest niska."),
            ]
            assert len(retry["messages"]) == 4

        sim.stop()
        await asyncio.wait_for(task, 3)

    asyncio.run(scenario())


async def _link_ok(http):
    return (await http.get("/health")).json()["link"]["state"] == "ok"


async def _has(drives, name):
    return name in await drives()


async def _lacks(drives, name):
    return name not in await drives()
