"""WebSocket link to the stackchan device.

Topology
--------
The device opens an outbound WebSocket to this gateway and keeps it alive
indefinitely (heartbeat). This gateway listens for that one connection and
exposes a `request()` API that MCP tools call to push commands to the device.

We assume **one** device (Panice has one stackchan). If a second connection
arrives, it replaces the first — last-connect-wins. This keeps the code small;
fan-out to multiple devices is YAGNI for now.

Wire format
-----------
Gateway → Device (request):
    {"id": 42, "method": "move", "params": {"x": 30, "y": 15, "speed": 50}}

Device → Gateway (response to request):
    {"id": 42, "ok": true, "result": {...}}     # success
    {"id": 42, "ok": false, "error": "..."}      # failure

Device → Gateway (unsolicited event, no id):
    {"event": "hello", "device_id": "..."}
    {"event": "audio_ready", "duration_ms": 3200}

This is intentionally NOT JSON-RPC 2.0 or the xiaozhi MCP wrapper — both add
ceremony we don't need. We use one shared id space, one connection, and
trust the device to be honest about ids it's seen.

Auth
----
The connecting device must send `Authorization: Bearer <STACKCHAN_TOKEN>` in
the WebSocket upgrade headers. Same token shape as old body-mcp, so existing
VPS .env can be reused.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

logger = logging.getLogger(__name__)

# How long to wait for a device response before giving up. Device commands
# should ack within a few seconds — TTS playback queues immediately, servo
# moves are < 1s. 10s gives generous slack.
RESPONSE_TIMEOUT = 10.0

# Bearer token the device must present in the WS upgrade Authorization header.
# Reuse the old body-mcp name STACKCHAN_TOKEN so the VPS .env carries over.
#
# Read at request time, not module load time: lets `.env` files loaded after
# this module imports still take effect, and avoids the test-ordering bug
# where importing this module before setting env in conftest captured "".
def _expected_token() -> str:
    return os.environ.get("STACKCHAN_TOKEN", "")


class DeviceLink:
    """Single-device WebSocket link with request/response + event semantics.

    Lifecycle:
        link = DeviceLink()
        await link.start(host="0.0.0.0", port=8765)   # background-listens
        result = await link.request("move", {"x": 30, "y": 0, "speed": 50})
        await link.stop()

    `request()` blocks until the device acks the matching id. If the device
    is offline or disconnects mid-flight, raises DeviceOffline.
    """

    def __init__(self) -> None:
        self._ws: ServerConnection | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._server: websockets.Server | None = None
        # Event subscribers — media_server uses this to know when device says
        # "audio_ready" so the MCP tool can pull the upload.
        self._event_listeners: list[asyncio.Queue[dict[str, Any]]] = []
        # Persistent log of unsolicited events, for the stackchan_events
        # tool. Subscriber queues above are transient (tools waiting on a
        # signal); this answers "did anything happen while nobody was on
        # the line?" — head pets, shakes, lifts, presence changes.
        self.event_log: deque[dict[str, Any]] = deque(maxlen=200)

    # ── Public API ────────────────────────────────────────────────────

    @property
    def online(self) -> bool:
        return self._ws is not None

    async def start(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        """Start the WebSocket server. Returns once listening — handlers
        run in the background on the same asyncio loop."""

        async def auth_check(connection: ServerConnection, request) -> Any:
            """Reject connection during the HTTP upgrade if bearer is wrong.
            websockets calls this with the request; returning a Response
            short-circuits the upgrade. Returning None continues normally."""
            expected = _expected_token()
            if not expected:
                # No token configured — allow all. Only safe for local dev.
                return None
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {expected}":
                logger.warning("Rejected device WS: bad/missing bearer")
                return connection.respond(401, "unauthorized\n")
            return None

        self._server = await serve(
            self._handle_connection,
            host=host,
            port=port,
            process_request=auth_check,
            # Keep the device's TCP connection alive over idle minutes. The
            # ws library sends pings automatically; this just adjusts cadence
            # so the gateway notices a dead device within ~30s rather than
            # waiting for the next request to time out.
            ping_interval=20,
            ping_timeout=10,
        )
        logger.info("DeviceLink listening on ws://%s:%d", host, port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(DeviceOffline("gateway shutting down"))
        self._pending.clear()

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request to the device and await its response.

        Returns the `result` field on success, raises DeviceError on
        device-reported failure, raises DeviceOffline if no device is
        connected or it disconnects before responding.
        """
        if not self._ws:
            raise DeviceOffline("no device connected")

        req_id = self._next_id
        self._next_id += 1
        msg = {"id": req_id, "method": method, "params": params or {}}

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        try:
            await self._ws.send(json.dumps(msg))
        except websockets.exceptions.ConnectionClosed as e:
            self._pending.pop(req_id, None)
            raise DeviceOffline("device disconnected mid-send") from e

        try:
            payload = await asyncio.wait_for(future, timeout=RESPONSE_TIMEOUT)
        except asyncio.TimeoutError as e:
            self._pending.pop(req_id, None)
            raise DeviceError(f"device did not ack '{method}' in {RESPONSE_TIMEOUT}s") from e

        if not payload.get("ok"):
            raise DeviceError(payload.get("error", "device returned ok=false"))
        return payload.get("result")

    def subscribe_events(self) -> asyncio.Queue[dict[str, Any]]:
        """Get a queue that receives all unsolicited device events.

        Used by tools that need to wait for an async device-initiated signal
        (e.g. `listen` waits for `audio_ready` after telling device to record).
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        self._event_listeners.append(q)
        return q

    def unsubscribe_events(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._event_listeners.remove(q)
        except ValueError:
            pass

    # ── Internal ──────────────────────────────────────────────────────

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """One device connected. If another is already connected, replace it."""
        if self._ws is not None:
            logger.warning("New device connection — closing previous one")
            try:
                await self._ws.close(code=1000, reason="replaced by new connection")
            except Exception:
                pass
        self._ws = ws
        logger.info("Device connected from %s", ws.remote_address)
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Device sent non-JSON: %r", raw[:200])
                    continue
                await self._dispatch(msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if self._ws is ws:
                self._ws = None
            # Fail any pending requests so callers don't hang.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(DeviceOffline("device disconnected"))
            self._pending.clear()
            logger.info("Device disconnected")

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route an incoming device message: response (has id) or event."""
        if "id" in msg:
            req_id = msg["id"]
            fut = self._pending.pop(req_id, None)
            if fut and not fut.done():
                fut.set_result(msg)
            else:
                logger.warning("Device responded to unknown id=%s", req_id)
            return
        if "event" in msg:
            entry = dict(msg)
            entry["received_at"] = time.time()
            self.event_log.append(entry)
            for q in self._event_listeners:
                # Non-blocking put — if a listener is full, drop the event for
                # that listener rather than stalling the WS read loop.
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    logger.warning("Event listener queue full; dropping event")
            return
        logger.warning("Device sent unknown message shape: %s", msg)


class DeviceOffline(RuntimeError):
    """Raised when no device is connected or it disconnects mid-request."""


class DeviceError(RuntimeError):
    """Raised when the device acked but reported failure."""
