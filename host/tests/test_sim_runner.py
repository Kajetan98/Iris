"""Klient sim.body przeciw prostemu serwerowi udającemu hosta."""

import asyncio
import json
import socket

import pytest
from websockets.asyncio.server import serve

from sim.body import SimRunner
from sim.model import SimConfig


class FakeHost:
    def __init__(self):
        self.received = []  # (numer połączenia, wiadomość)
        self.sockets = []

    async def handler(self, ws):
        self.sockets.append(ws)
        n = len(self.sockets)
        async for msg in ws:
            self.received.append((n, msg))

    def frames(self, conn=None):
        return [
            json.loads(m) for n, m in self.received
            if isinstance(m, str) and (conn is None or n == conn)
        ]

    def types(self, conn=None):
        return [f["type"] for f in self.frames(conn)]


async def until(pred, timeout=4.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("warunek nie został spełniony w czasie")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def runner_for(port, **kw):
    cfg = SimConfig(heartbeat_interval_s=0.05, sensor_period_s=0.5)
    return SimRunner(cfg, f"ws://127.0.0.1:{port}/ws", say=lambda s: None, **kw)


async def start(runner):
    return asyncio.create_task(runner.run())


async def finish(runner, task):
    runner.stop()
    await asyncio.wait_for(task, 3)


def test_boot_comes_first_then_heartbeats():
    async def scenario():
        host = FakeHost()
        async with serve(host.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner = runner_for(port)
            task = await start(runner)
            await until(lambda: host.types().count("body.heartbeat") >= 3)
            assert host.types()[0] == "system.boot"
            await finish(runner, task)

    asyncio.run(scenario())


def test_events_buffered_before_first_connection_are_delivered():
    async def scenario():
        port = free_port()
        runner = runner_for(port)
        task = await start(runner)
        await asyncio.sleep(0.2)
        runner.command("press")
        host = FakeHost()
        async with serve(host.handler, "127.0.0.1", port):
            await until(lambda: "button" in host.types())
            types = host.types()
            assert types.index("system.boot") < types.index("button")
            await until(lambda: "body.heartbeat" in host.types())
            types = host.types()
            assert types.index("body.heartbeat") > types.index("button")
            assert runner.model.dropped == 0
        await finish(runner, task)

    asyncio.run(scenario())


def test_action_is_answered_with_ack():
    async def scenario():
        host = FakeHost()
        async with serve(host.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner = runner_for(port)
            task = await start(runner)
            await until(lambda: host.sockets)
            action = {"v": 1, "t": "action", "id": "h1", "ts": 5, "type": "haptic", "body": {"pattern": "warn"}}
            await host.sockets[0].send(json.dumps(action))
            await until(lambda: "haptic" in host.types())
            ack = next(f for f in host.frames() if f["t"] == "ack")
            assert ack["body"] == {"ref": "h1", "ok": True}
            assert runner.model.haptic_log == ["warn"]
            await finish(runner, task)

    asyncio.run(scenario())


def test_playback_audio_then_speak():
    async def scenario():
        host = FakeHost()
        async with serve(host.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner = runner_for(port)
            task = await start(runner)
            await until(lambda: host.sockets)
            await host.sockets[0].send(b"\x02\x00\x09\x00\x00" + b"opus-data")
            await host.sockets[0].send(json.dumps(
                {"v": 1, "t": "action", "id": "h2", "ts": 1, "type": "speak", "body": {"audio_id": 9}}))
            await until(lambda: any(f["t"] == "ack" for f in host.frames()))
            assert next(f for f in host.frames() if f["t"] == "ack")["body"]["ok"] is True
            assert runner.model.played == [(9, 9)]
            await finish(runner, task)

    asyncio.run(scenario())


def test_drop_buffers_events_and_reconnects_in_order():
    async def scenario():
        host = FakeHost()
        async with serve(host.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner = runner_for(port)
            task = await start(runner)
            await until(lambda: runner.connected)
            runner.command("drop 0.4")
            await until(lambda: not runner.connected)
            runner.command("press")
            runner.command("local mute_mic")
            await until(lambda: runner.connections == 2)
            second = host.types(2)
            assert second[:2] == ["button", "local.command"]  # zaległe zdarzenia przed bieżącym ruchem
            assert "system.boot" not in second  # to samo ciało, nie restart
            await until(lambda: "body.heartbeat" in host.types(2))
            await finish(runner, task)

    asyncio.run(scenario())


def test_buffer_limit_drops_oldest_during_outage():
    async def scenario():
        host = FakeHost()
        async with serve(host.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = SimConfig(heartbeat_interval_s=0.05, buffer_limit=3)
            runner = SimRunner(cfg, f"ws://127.0.0.1:{port}/ws", say=lambda s: None)
            task = await start(runner)
            await until(lambda: runner.connected)
            runner.command("drop 0.4")
            await until(lambda: not runner.connected)
            for i in range(5):
                runner.command(f"local c{i}")
            await until(lambda: runner.connections == 2)
            cmds = [f["body"]["cmd"] for f in host.frames(2) if f["type"] == "local.command"]
            assert cmds == ["c2", "c3", "c4"]
            assert runner.model.dropped == 2
            await finish(runner, task)

    asyncio.run(scenario())


def test_reconnects_after_host_closes_connection():
    async def scenario():
        host = FakeHost()
        async with serve(host.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner = runner_for(port)
            task = await start(runner)
            await until(lambda: host.sockets)
            await host.sockets[0].close(code=4000, reason="replaced")
            await until(lambda: runner.connections == 2, timeout=5)
            await until(lambda: "body.heartbeat" in host.types(2))
            await finish(runner, task)

    asyncio.run(scenario())


def test_script_runs_commands_on_time():
    async def scenario():
        host = FakeHost()
        async with serve(host.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner = runner_for(port, script=[(0.2, "press long"), (0.3, "fault gps no_fix")])
            task = await start(runner)
            await until(lambda: "body.fault" in host.types())
            kinds = [f["body"]["kind"] for f in host.frames() if f["type"] == "button"]
            assert kinds == ["long"]
            await finish(runner, task)

    asyncio.run(scenario())


def test_quit_command_stops_the_runner():
    async def scenario():
        host = FakeHost()
        async with serve(host.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner = runner_for(port, script=[(0.1, "quit")])
            task = await start(runner)
            await asyncio.wait_for(task, 3)

    asyncio.run(scenario())


def test_stops_cleanly_without_any_server():
    async def scenario():
        runner = runner_for(free_port())
        task = await start(runner)
        await asyncio.sleep(0.2)
        await finish(runner, task)
        assert runner.connections == 0

    asyncio.run(scenario())
