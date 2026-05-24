"""HTTP media side-channel between gateway and device.

WebSocket carries control (small JSON). Bulk audio/image bytes go over HTTP
because they're large and either need range-pull semantics (TTS WAV) or
multipart upload (recordings / snapshots) — both of which WS makes awkward.

Three endpoints:

1. `GET /audio/<filename>` — serves a TTS WAV from AUDIO_DIR (tts.py writes
   here). The gateway tells the device to play `https://body/audio/<file>.wav`
   and the device pulls it. **Auth**: bearer token, so a stranger who learns
   the URL can't hammer our TTS endpoint.

2. `POST /upload/audio` — device uploads a recording (after we sent it a
   `listen` command and it emitted `audio_ready`). Multipart with field `file`.
   Saved to CAPTURE_DIR; path returned in response.

3. `POST /upload/photo` — device uploads a JPEG (after we sent `snapshot`).
   Multipart with field `file`. Saved to CAPTURE_DIR.

All three live behind the same Caddy reverse-proxy as the WS server, so a
single domain + single bearer token suffices (we reuse STACKCHAN_TOKEN — same
as the WS auth — because the device is the only legit caller of all three).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", "/tmp/stackchan_audio"))
CAPTURE_DIR = Path(os.environ.get("CAPTURE_DIR", os.path.expanduser("~/.stackchan/captures")))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

# Reuse STACKCHAN_TOKEN: device is the only legitimate caller for all three
# endpoints, and forcing it to track two separate tokens buys nothing.
#
# Read at request time — same reason as device_link: lets `.env` loaded after
# this module imports still take effect, and avoids test-ordering gotchas.
def _expected_token() -> str:
    return os.environ.get("STACKCHAN_TOKEN", "")


def _require_bearer(request: web.Request) -> web.Response | None:
    """Return a 401 response if the bearer header doesn't match, else None.

    When STACKCHAN_TOKEN is empty we allow everything — only safe for local
    dev. The deploy script sets it; don't ship empty.
    """
    expected = _expected_token()
    if not expected:
        return None
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {expected}":
        return web.Response(status=401, text="unauthorized\n")
    return None


async def handle_audio_get(request: web.Request) -> web.StreamResponse:
    """Serve a TTS WAV from AUDIO_DIR. Filename is path-only to prevent
    traversal — we strip directories from the request and re-anchor in
    AUDIO_DIR ourselves."""
    if (deny := _require_bearer(request)) is not None:
        return deny

    raw_name = request.match_info["filename"]
    safe_name = os.path.basename(raw_name)  # strip any "../" etc.
    path = AUDIO_DIR / safe_name
    if not path.exists() or not path.is_file():
        return web.Response(status=404, text="not found\n")
    return web.FileResponse(path)


async def handle_audio_upload(request: web.Request) -> web.Response:
    """Device uploads a recording. Multipart field `file` is the WAV."""
    if (deny := _require_bearer(request)) is not None:
        return deny

    reader = await request.multipart()
    saved_path: Path | None = None

    async for part in reader:
        if part.name == "file":
            ts = int(time.time() * 1000)
            saved_path = CAPTURE_DIR / f"rec_{ts}.wav"
            with open(saved_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(8192)
                    if not chunk:
                        break
                    f.write(chunk)

    if saved_path is None or not saved_path.exists():
        return web.json_response({"error": "no file field"}, status=400)

    size = saved_path.stat().st_size
    logger.info("Recording uploaded: %s (%d bytes)", saved_path, size)
    return web.json_response({"path": str(saved_path), "size_bytes": size})


async def handle_photo_upload(request: web.Request) -> web.Response:
    """Device uploads a JPEG. Multipart field `file` is the image."""
    if (deny := _require_bearer(request)) is not None:
        return deny

    reader = await request.multipart()
    saved_path: Path | None = None

    async for part in reader:
        if part.name == "file":
            ts = int(time.time() * 1000)
            saved_path = CAPTURE_DIR / f"capture_{ts}.jpg"
            with open(saved_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(8192)
                    if not chunk:
                        break
                    f.write(chunk)

    if saved_path is None or not saved_path.exists():
        return web.json_response({"error": "no file field"}, status=400)

    size = saved_path.stat().st_size
    logger.info("Photo uploaded: %s (%d bytes)", saved_path, size)
    return web.json_response({"path": str(saved_path), "size_bytes": size})


def build_app() -> web.Application:
    """Build the aiohttp app exposing the three media endpoints."""
    app = web.Application(client_max_size=10 * 1024 * 1024)  # 10MB cap on uploads
    app.router.add_get("/audio/{filename}", handle_audio_get)
    app.router.add_post("/upload/audio", handle_audio_upload)
    app.router.add_post("/upload/photo", handle_photo_upload)
    return app


async def run_in_loop(host: str = "0.0.0.0", port: int = 8766) -> web.AppRunner:
    """Start the media server on the current asyncio loop. Returns the
    runner so the caller can clean up. Doesn't block."""
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Media server listening on http://%s:%d", host, port)
    return runner
