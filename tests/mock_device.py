"""Mock stackchan device — connects to gateway WS, acks commands.

Stands in for real firmware during local end-to-end tests. Speaks the same
wire format documented in `mcp-server/device_link.py`:

    Gateway -> us:    {"id": N, "method": "...", "params": {...}}
    Us -> gateway:    {"id": N, "ok": true/false, "result"|"error": ...}
    Us -> gateway:    {"event": "...", ...}     (unsolicited)

Behaviors implemented:
- Acks all known methods (play / move / nod / shake / home / face / led_* / status).
- For `listen`: posts a fake WAV to /upload/audio, then emits `audio_ready`
  with the path returned by the upload endpoint.
- For `snapshot`: posts a fake JPEG to /upload/photo, then emits `photo_ready`.
- For `play`: pretends to fetch the WAV URL and confirms it was reachable.

Run standalone:
    python tests/mock_device.py --gateway ws://127.0.0.1:8765/ \\
        --media http://127.0.0.1:8766 --token test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import wave
from pathlib import Path

import aiohttp
import websockets

logger = logging.getLogger("mock_device")


# Minimal 1-second silent 24kHz mono WAV — enough for STT to return empty
# without erroring, and small enough that uploads are instant in tests.
def _make_silent_wav(seconds: float = 1.0, rate: int = 24000) -> bytes:
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


# Tiny synthetic JPEG (a 1x1 white pixel). Real firmware sends 320x240 JPEGs
# from the GC0308 — for test purposes the bytes just need to be a valid JPEG
# so the gateway can pass them back to the MCP client unchanged.
_PIXEL_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707"
    "0709090808080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e27202229"
    "2c231c1c2837292c30313434341f27393d38323c2e333432ffdb0043010909090c0b"
    "0c180d0d1832211c213232323232323232323232323232323232323232323232323232"
    "32323232323232323232323232323232323232323232323232ffc00011080001000103"
    "012200021101031101ffc4001f0000010501010101010100000000000000000102030"
    "405060708090a0bffc400b5100002010303020403050504040000017d010203000411"
    "05122131410613516107227114328191a1082342b1c11552d1f02433627282090a161"
    "718191a25262728292a3435363738393a434445464748494a535455565758595a6364"
    "65666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a"
    "5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2"
    "e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010"
    "00000000000000102030405060708090a0bffc400b51100020102040403040705040400"
    "010277000102031104052131061241510761711322328108144291a1b1c109233352f0"
    "156272d10a162434e125f11718191a262728292a35363738393a434445464748494a5"
    "35455565758595a636465666768696a737475767778797a82838485868788898a9293"
    "9495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad"
    "2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002"
    "11031101003f00fbfcffd9"
)


class MockDevice:
    def __init__(self, ws_url: str, media_url: str, token: str, device_id: str = "mock-01"):
        self.ws_url = ws_url
        self.media_url = media_url.rstrip("/")
        self.token = token
        self.device_id = device_id
        self.ws: websockets.ClientConnection | None = None
        # Track what we've been asked, so tests can assert on it.
        self.received: list[dict] = []

    async def run(self) -> None:
        """Connect and serve until the gateway closes the connection."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with websockets.connect(
            self.ws_url, additional_headers=headers, ping_interval=20
        ) as ws:
            self.ws = ws
            logger.info("connected to %s", self.ws_url)
            await self._send({"event": "hello", "device_id": self.device_id})
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    self.received.append(msg)
                    await self._handle(msg)
            except websockets.exceptions.ConnectionClosed:
                logger.info("gateway closed connection")

    async def _send(self, msg: dict) -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps(msg))

    async def _ack(self, req_id: int, result=None) -> None:
        await self._send({"id": req_id, "ok": True, "result": result})

    async def _nack(self, req_id: int, error: str) -> None:
        await self._send({"id": req_id, "ok": False, "error": error})

    async def _handle(self, msg: dict) -> None:
        req_id = msg.get("id")
        if req_id is None:
            logger.warning("got non-request: %s", msg)
            return
        method = msg.get("method", "")
        params = msg.get("params") or {}

        try:
            if method == "play":
                # Pretend to fetch the URL — verifies media server serves it.
                url = params["voice_url"]
                async with aiohttp.ClientSession() as s:
                    headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
                    async with s.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            await self._nack(req_id, f"audio fetch {resp.status}")
                            return
                        await resp.read()  # discard
                await self._ack(req_id, {"played_bytes": "ok"})

            elif method in ("nod", "shake", "home"):
                await self._ack(req_id, {"gesture": method})

            elif method == "move":
                await self._ack(req_id, {"x": params.get("x"), "y": params.get("y")})

            elif method == "face":
                await self._ack(req_id, {"face": params.get("expression")})

            elif method.startswith("led_"):
                await self._ack(req_id, {"led_op": method})

            elif method == "status":
                await self._ack(req_id, f"device={self.device_id} uptime=mock")

            elif method == "listen":
                # Ack the listen command immediately. Then simulate the
                # record→upload→event sequence after a short delay.
                await self._ack(req_id, {"recording_started": True})
                duration_ms = int(params.get("duration_ms", 5000))
                asyncio.create_task(self._do_listen_upload(duration_ms))

            elif method == "snapshot":
                await self._ack(req_id, {"snapshot_started": True})
                asyncio.create_task(self._do_snapshot_upload())

            else:
                await self._nack(req_id, f"unknown method {method}")
        except Exception as e:
            logger.exception("handler crashed")
            await self._nack(req_id, f"handler crashed: {e}")

    async def _do_listen_upload(self, duration_ms: int) -> None:
        # Simulate "I recorded for the requested time" — but compressed so
        # tests are fast. Real firmware would actually record.
        await asyncio.sleep(min(0.1, duration_ms / 10000))
        wav_bytes = _make_silent_wav(seconds=1.0)
        async with aiohttp.ClientSession() as s:
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            data = aiohttp.FormData()
            data.add_field("file", wav_bytes, filename="rec.wav", content_type="audio/wav")
            async with s.post(f"{self.media_url}/upload/audio", data=data, headers=headers) as r:
                resp = await r.json()
                path = resp.get("path", "")
        await self._send({"event": "audio_ready", "path": path, "duration_ms": duration_ms})

    async def _do_snapshot_upload(self) -> None:
        await asyncio.sleep(0.05)
        async with aiohttp.ClientSession() as s:
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            data = aiohttp.FormData()
            data.add_field("file", _PIXEL_JPEG, filename="snap.jpg", content_type="image/jpeg")
            async with s.post(f"{self.media_url}/upload/photo", data=data, headers=headers) as r:
                resp = await r.json()
                path = resp.get("path", "")
        await self._send({"event": "photo_ready", "path": path})


async def _amain(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    dev = MockDevice(args.gateway, args.media, args.token)
    while True:
        try:
            await dev.run()
        except (ConnectionRefusedError, OSError) as e:
            logger.info("gateway down (%s), retrying in 2s", e)
            await asyncio.sleep(2)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--gateway", default="ws://127.0.0.1:8765/")
    p.add_argument("--media", default="http://127.0.0.1:8766")
    p.add_argument("--token", default="test")
    asyncio.run(_amain(p.parse_args()))
