"""End-to-end test of the WS protocol between gateway and a fake device.

This doesn't need real hardware — we spin up DeviceLink on a port, connect a
fake device coroutine to it, and verify request/response and event flows.

Run: uv run pytest tests/test_device_link.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-server"))

from device_link import DeviceError, DeviceLink, DeviceOffline  # noqa: E402


# Bind to 0 so the OS picks a free port each test → no flakiness from leftover
# state on a hard-coded port between runs.
async def _start_link() -> tuple[DeviceLink, int]:
    link = DeviceLink()
    await link.start(host="127.0.0.1", port=0)
    # Pull the actual bound port back out of the websockets server
    port = link._server.sockets[0].getsockname()[1]
    return link, port


async def _fake_device(port: int, handler) -> None:
    """Connect to ws://127.0.0.1:{port}/ as a fake device. `handler` is an
    async callable taking the websocket; it does whatever the test needs."""
    async with websockets.connect(f"ws://127.0.0.1:{port}/") as ws:
        await handler(ws)


@pytest.mark.asyncio
async def test_request_response_happy_path():
    """Gateway sends 'move' request; fake device acks. Gateway awaits result."""
    link, port = await _start_link()

    async def device(ws):
        msg = json.loads(await ws.recv())
        assert msg["method"] == "move"
        assert msg["params"] == {"x": 30, "y": 15, "speed": 50}
        await ws.send(json.dumps({"id": msg["id"], "ok": True, "result": "moved"}))
        # Stay connected so the test can finish cleanly
        await asyncio.sleep(0.5)

    device_task = asyncio.create_task(_fake_device(port, device))
    # Give the device a moment to connect before we send.
    await asyncio.sleep(0.1)

    try:
        result = await link.request("move", {"x": 30, "y": 15, "speed": 50})
        assert result == "moved"
    finally:
        device_task.cancel()
        await asyncio.gather(device_task, return_exceptions=True)
        await link.stop()


@pytest.mark.asyncio
async def test_device_error_propagates():
    """Device replies ok=false → gateway raises DeviceError with the message."""
    link, port = await _start_link()

    async def device(ws):
        msg = json.loads(await ws.recv())
        await ws.send(json.dumps({
            "id": msg["id"], "ok": False, "error": "servo not ready",
        }))
        await asyncio.sleep(0.5)

    device_task = asyncio.create_task(_fake_device(port, device))
    await asyncio.sleep(0.1)

    try:
        with pytest.raises(DeviceError, match="servo not ready"):
            await link.request("move", {"x": 0, "y": 0, "speed": 50})
    finally:
        device_task.cancel()
        await asyncio.gather(device_task, return_exceptions=True)
        await link.stop()


@pytest.mark.asyncio
async def test_offline_when_no_device():
    """No device connected → request raises DeviceOffline immediately."""
    link, _port = await _start_link()
    try:
        with pytest.raises(DeviceOffline):
            await link.request("move", {"x": 0})
    finally:
        await link.stop()


@pytest.mark.asyncio
async def test_event_subscription():
    """Device pushes an event with no id → it lands on every subscriber queue."""
    link, port = await _start_link()

    async def device(ws):
        # Wait a beat so the subscriber is registered before we publish.
        await asyncio.sleep(0.2)
        await ws.send(json.dumps({"event": "audio_ready", "path": "rec_42.wav"}))
        await asyncio.sleep(0.5)

    device_task = asyncio.create_task(_fake_device(port, device))
    await asyncio.sleep(0.1)

    q = link.subscribe_events()
    try:
        event = await asyncio.wait_for(q.get(), timeout=2)
        assert event["event"] == "audio_ready"
        assert event["path"] == "rec_42.wav"
    finally:
        link.unsubscribe_events(q)
        device_task.cancel()
        await asyncio.gather(device_task, return_exceptions=True)
        await link.stop()


@pytest.mark.asyncio
async def test_new_connection_replaces_old():
    """Second device connects → first one is closed, second becomes active."""
    link, port = await _start_link()

    first_closed = asyncio.Event()

    async def first_device(ws):
        # `async for` over a websocket exits silently when the peer closes —
        # it does NOT raise ConnectionClosed in that direction. So signal
        # "closed" by reaching the end of iteration.
        async for _ in ws:
            pass
        first_closed.set()

    async def second_device(ws):
        msg = json.loads(await ws.recv())
        assert msg["method"] == "home"
        await ws.send(json.dumps({"id": msg["id"], "ok": True}))
        await asyncio.sleep(0.5)

    t1 = asyncio.create_task(_fake_device(port, first_device))
    await asyncio.sleep(0.1)
    t2 = asyncio.create_task(_fake_device(port, second_device))
    await asyncio.sleep(0.2)

    try:
        # First should be closed by now
        assert first_closed.is_set(), "first device wasn't kicked out"
        # Second should be reachable
        await link.request("home")
    finally:
        for t in (t1, t2):
            t.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)
        await link.stop()
