"""Symulator przeciw prawdziwemu hostowi (uvicorn w wątku, skrócone progi)."""

import asyncio
import inspect
import socket
import threading

import httpx
import pytest
import uvicorn

from host.api.main import create_app
from host.core.config import Config
from sim.body import SimRunner
from sim.model import SimConfig

FAST = {
    "store": {"path": ":memory:"},
    "link": {"timeout_s": 0.8, "recover_s": 0.4},
    "drives": {
        "battery_low": {
            "direction": "below", "enter": 20, "exit": 25, "enter_hold_s": 0.3, "exit_hold_s": 0.3,
        }
    },
    "api": {"tick_interval_s": 0.1, "ack_timeout_s": 2.0},
}


@pytest.fixture
def host():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(Config.model_validate(FAST))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        threading.Event().wait(0.05)
    assert server.started, "uvicorn nie wystartował"
    yield app, port
    server.should_exit = True
    thread.join(5)


async def until(fn, timeout=5.0):
    """Czeka, aż fn() (zwykła lub async) zwróci prawdę."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        value = fn()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("warunek nie został spełniony w czasie")


def test_sim_against_real_host(host):
    app, port = host

    async def scenario():
        sim = SimRunner(
            SimConfig(heartbeat_interval_s=0.1, sensor_period_s=0.5),
            f"ws://127.0.0.1:{port}/ws",
            say=lambda s: None,
        )
        task = asyncio.create_task(sim.run())
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5) as http:

            async def self_state():
                return (await http.get("/self")).json()

            async def link_is(state):
                return (await http.get("/health")).json()["link"]["state"] == state

            async def drive_active(name, expected=True):
                return (name in (await self_state())["self"]["drives"]) == expected

            async def events(*types):
                r = await http.get("/events", params=[("type", t) for t in types])
                return r.json()["events"]

            # połączenie i stan z heartbeatów
            await until(lambda: link_is("ok"))
            assert (await self_state())["body"]["batt_pct"] == 71
            boots = await events("system.boot")
            assert len(boots) == 1 and boots[0]["data"]["fw"] == "sim-0.1"

            # spadek baterii: drive i tryb saving, potem powrót
            sim.command("batt 10")
            await until(lambda: drive_active("battery_low"))
            assert (await self_state())["self"]["mode"] == "saving"
            sim.command("batt 50")
            await until(lambda: drive_active("battery_low", False))

            # usterka sensora i przycisk
            sim.command("fault gps no_fix")
            sim.command("press")
            await until(lambda: events("button"))
            assert (await self_state())["body"]["sensors_down"] == ["gps"]

            # akcje przez REST: ack z ciała, także odmowa
            ok = await http.post("/actions", json={"type": "haptic", "body": {"pattern": "warn"}})
            assert ok.status_code == 200 and ok.json()["ack"]["ok"] is True
            assert sim.model.haptic_log == ["warn"]
            bad = await http.post("/actions", json={"type": "gpio.set", "body": {"pin": 99, "value": 1}})
            assert bad.status_code == 200
            assert bad.json()["ack"] == {"ref": bad.json()["id"], "ok": False, "err": "pin not allowed"}

            # zerwanie łącza: link_lost, zdarzenie z bufora, powrót
            sim.command("drop 1.5")
            await until(lambda: not sim.connected)
            sim.command("press long")
            await until(lambda: drive_active("link_lost"))
            await until(lambda: link_is("ok"), timeout=8)
            await until(lambda: drive_active("link_lost", False), timeout=8)

            seen = [
                (e["type"], e["data"].get("kind"))
                for e in await events("link.lost", "link.restored", "button")
            ]
            # press, utrata łącza, long z bufora po powrocie, link.restored od pierwszego heartbeatu
            assert seen == [
                ("button", "press"),
                ("link.lost", None),
                ("button", "long"),
                ("link.restored", None),
            ]
            assert sim.model.dropped == 0

            # wypowiedź: zdarzenia dochodzą, ramki audio nie psują połączenia
            sim.command("say 200")
            await until(lambda: events("audio.utterance.end"))
            assert await link_is("ok")

        sim.stop()
        await asyncio.wait_for(task, 3)

    asyncio.run(scenario())
    assert app.state.service.rejected_frames == 0  # każda ramka symulatora przeszła dekoder hosta
