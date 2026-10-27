"""Symulator jako jedno z kilku urządzeń (docs/spec.md, sekcja 1)."""

import asyncio
import json

import httpx

from host.tests.test_sim_e2e import host, until  # noqa: F401  (fixture)
from sim.body import SimRunner, parse_args, with_device
from sim.model import BodyModel, SimConfig


def action(type_, body, id_="h1"):
    return json.dumps({"v": 1, "t": "action", "id": id_, "ts": 0, "type": type_, "body": body})


def acks(model):
    return [json.loads(o.data)["body"] for o in model.take_out() if isinstance(o.data, str)]


def test_device_active_action_and_reconnect():
    m = BodyModel(SimConfig(), 0.0)
    m.take_out()
    m.on_host_text(action("device.active", {"active": False}), 1.0)
    assert acks(m) == [{"ref": "h1", "ok": True}] and m.active is False
    assert "nieaktywne" in m.command("say 500", 1.0) and m.take_out() == []
    assert "aktywne urządzenie: nie" in m.command("status", 1.0)
    for bad in ({}, {"active": 1}, {"active": "false"}, {"active": None}):
        m.on_host_text(action("device.active", bad), 1.0)
        assert acks(m) == [{"ref": "h1", "ok": False, "err": "bad_body"}]
    assert m.active is False
    m.on_connect()  # nowe połączenie: aktywne, dopóki host nie powie inaczej
    assert m.active is True


def test_activate_is_sent_but_never_buffered():
    m = BodyModel(SimConfig(), 0.0)
    m.take_out()
    m.command("activate", 2.0)
    (out,) = m.take_out()
    frame = json.loads(out.data)
    assert (frame["type"], frame["body"], out.bufferable) == ("device.activate", {}, False)
    m.stash([out])
    assert len(m.buffer) == 0


def test_device_parameter_in_url():
    assert with_device("ws://localhost:8000/ws", "laptop") == "ws://localhost:8000/ws?device=laptop"
    assert with_device("ws://h/ws?x=1&device=a", "pi-kuchnia") == "ws://h/ws?x=1&device=pi-kuchnia"
    assert parse_args(["--device", "laptop"]).device == "laptop"


def test_two_simulators_switch_between_each_other(host):  # noqa: F811
    app, port = host
    url = f"ws://127.0.0.1:{port}/ws"

    async def scenario():
        hermes = SimRunner(SimConfig(heartbeat_interval_s=0.1, seed=1), url, say=lambda s: None)
        laptop = SimRunner(SimConfig(heartbeat_interval_s=0.1, seed=2, batt_pct=15), with_device(url, "laptop"),
                           say=lambda s: None)
        tasks = [asyncio.create_task(hermes.run())]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5) as http:
            await until(lambda: hermes.connected)
            tasks.append(asyncio.create_task(laptop.run()))
            await until(lambda: laptop.connected and not laptop.model.active)

            async def devices():
                return (await http.get("/devices")).json()

            async def drives():
                return (await http.get("/self")).json()["self"]["drives"]

            body = await devices()
            assert body["active"] == "hermes" and [d["id"] for d in body["devices"]] == ["hermes", "laptop"]
            assert (await drives()) == []  # bateria laptopa (15%) nie liczy się, gdy jest w spoczynku

            assert (await http.post("/devices/laptop/activate")).status_code == 200
            await until(lambda: laptop.model.active and not hermes.model.active)
            await until(lambda: drives_contain(http, "battery_low"))
            assert "nieaktywne" in hermes.model.command("say 300", 0)

            hermes.command("activate")  # przycisk na nieaktywnym urządzeniu
            await until(lambda: hermes.model.active and not laptop.model.active)
            await until(lambda: drives_empty(http))
            events = (await http.get("/events", params={"type": "device.switch"})).json()["events"]
            assert [(e["data"]["to"], e["data"]["by"]) for e in events] == [
                ("hermes", "first"), ("laptop", "rest"), ("hermes", "device"),
            ]
            exits = (await http.get("/events", params={"type": "drive.exit"})).json()["events"]
            assert [(e["device"], e["data"]) for e in exits] == [
                ("laptop", {"drive": "battery_low", "reason": "device_switch"}),
            ]
        hermes.stop()
        laptop.stop()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


async def drives_contain(http, drive):
    return drive in (await http.get("/self")).json()["self"]["drives"]


async def drives_empty(http):
    return (await http.get("/self")).json()["self"]["drives"] == []
