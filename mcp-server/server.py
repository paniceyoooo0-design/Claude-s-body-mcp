"""Claude's body MCP — gateway for stackchan over outbound WS.

Topology
--------
This gateway lives on Panice's VPS (body.aerogelovepanice.com). The stackchan
device opens an outbound WebSocket to wss://body/ws and keeps it alive. Any
Claude window (Code / Desktop / browser) connects to this gateway over
streamable-http at https://body/mcp/ and calls tools; the gateway translates
each tool call into a control message pushed down the device WS.

Why this design vs migratorywhale's original HTTP-server-on-device:
- Device is behind home NAT with no static IP. Outbound WS is the only NAT-
  friendly path.
- Panice has no always-on home machine to act as a LAN proxy.
- We keep migratorywhale's clean intent-level tools (nod / face / etc) and
  pair them with the WS-outbound topology from her old body-mcp project.

See [[project_stackchan_mcp]] memory for full direction history.

Tools (13 total)
----------------
migratorywhale-inspired (9):
    stackchan_say / listen / see / face / move / nod / shake / home / status
LED set (4, from old body-mcp because migratorywhale didn't expose LEDs):
    stackchan_set_led / set_all_leds / set_leds / clear_leds

TTS+STT both go through ElevenLabs (Panice designed a voice; voice_id is in
gateway/.env). Lip-sync is on-device: firmware reads audio amplitude in real
time and drives the m5stack-avatar mouth, so the gateway doesn't have to ship
visemes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP, Image

from device_link import DeviceError, DeviceLink, DeviceOffline
from media_server import AUDIO_DIR, CAPTURE_DIR, run_in_loop as run_media_server
from stt import STTError, transcribe
from tts import TTSError, synthesize

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("stackchan")

# ── Config ──────────────────────────────────────────────────────────────────
WS_PORT = int(os.environ.get("WS_PORT", 8765))
MEDIA_PORT = int(os.environ.get("MEDIA_PORT", 8766))
# Public base URL the device uses to fetch TTS audio. In prod this is
# https://body.aerogelovepanice.com (Caddy reverse-proxies /audio/* to the
# media server). For local dev you can set it to http://<mac-lan-ip>:8766.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8766")
LISTEN_DEFAULT_MS = int(os.environ.get("LISTEN_DEFAULT_MS", 5000))

# ── Singletons (one device, one link) ───────────────────────────────────────
link = DeviceLink()

# When FastMCP runs in stdio mode, lifespan starts the WS+media servers
# directly on the FastMCP asyncio loop. In streamable-http mode same thing —
# FastMCP supports lifespan in both transports.


@asynccontextmanager
async def _bringup(_server):
    """Bring the device WS server + media HTTP server up once per process.

    Used as the lifespan for two transports:
    - stdio: FastMCP's per-session lifespan IS the per-process lifespan (one
      session per stdio mcp.run() call).
    - streamable-http: attached to the outer Starlette app, NOT to FastMCP.
      FastMCP's session_manager spins up a new "session" per MCP request and
      runs its lifespan each time — putting WS+media bind here would crash
      every request after the first with EADDRINUSE on port 8765.
    """
    await link.start(host="0.0.0.0", port=WS_PORT)
    media_runner = await run_media_server(host="0.0.0.0", port=MEDIA_PORT)
    logger.info("Gateway up: WS on %d, media on %d, public base %s",
                WS_PORT, MEDIA_PORT, PUBLIC_BASE_URL)
    try:
        yield {}
    finally:
        await media_runner.cleanup()
        await link.stop()


mcp = FastMCP(
    "stackchan",
    # No lifespan here — see _bringup docstring. stdio mode wires it below;
    # http mode wires it on the outer Starlette app.
    # json_response=True: POST /mcp returns plain JSON-RPC instead of FastMCP's
    # default text/event-stream paired-stream response. SSE mode times out
    # behind Caddy because the GET-side machinery is fragile in reverse-proxy
    # setups; plain JSON is what every short-lived tool call needs.
    json_response=True,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _audio_url(wav_path: Path) -> str:
    """Build the device-facing URL for a TTS WAV in AUDIO_DIR."""
    return f"{PUBLIC_BASE_URL.rstrip('/')}/audio/{wav_path.name}"


async def _await_event(event_name: str, timeout: float = 15.0) -> dict:
    """Subscribe to device events; return the first matching one or raise."""
    q = link.subscribe_events()
    try:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"no {event_name!r} in {timeout}s")
            msg = await asyncio.wait_for(q.get(), timeout=remaining)
            if msg.get("event") == event_name:
                return msg
    finally:
        link.unsubscribe_events(q)


def _err(msg: str) -> str:
    """Format a user-visible error string consistent across tools."""
    return f"❌ {msg}"


# ── Tools: speech ───────────────────────────────────────────────────────────


@mcp.tool()
async def stackchan_say(text: str, lang: str = "zh") -> str:
    """Speak through Stack-chan's speaker using Panice's ElevenLabs voice.

    text: what to say (any length up to ElevenLabs limits ~5000 chars)
    lang: 'zh' / 'en' / 'ja' — hint only; ElevenLabs auto-detects.
    """
    try:
        wav_path = synthesize(text, lang)
    except TTSError as e:
        return _err(f"TTS failed: {e}")
    url = _audio_url(wav_path)
    try:
        await link.request("play", {"voice_url": url})
    except DeviceOffline:
        return _err("Stack-chan offline (no WS connection)")
    except DeviceError as e:
        return _err(f"Device refused play: {e}")
    preview = text[:60] + ("…" if len(text) > 60 else "")
    return f'🗣️  Saying: "{preview}" ({len(text)} chars, {lang})'


@mcp.tool()
async def stackchan_listen(duration_ms: int = LISTEN_DEFAULT_MS, lang: str = "zh") -> str:
    """Record from Stack-chan's mic and transcribe.

    duration_ms: how long to record (default 5000ms / 5s)
    lang: hint for STT ('zh' / 'en' / 'ja'); auto-detect if omitted.

    Flow: gateway → 'listen' → device records → device uploads WAV →
    device emits 'audio_ready' event with the saved filename → gateway
    runs ElevenLabs Scribe → returns transcript.
    """
    duration_ms = max(500, min(30000, duration_ms))  # clamp to sane range
    # The listen RPC is best-effort. Device may be mid-upload (HTTPS POST
    # blocks the WS event loop on ESP32) and not ack in time — that's OK,
    # the mic is always running with VAD anyway. We only fail hard if the
    # device is fully offline (no WS connection at all).
    if not link.online:
        return _err("Stack-chan offline (no WS connection)")
    try:
        await link.request("listen", {"duration_ms": duration_ms})
    except DeviceError as e:
        logger.info("listen RPC didn't ack (%s) — proceeding to wait for upload", e)

    # Device will upload via POST /upload/audio, then emit `audio_ready` with
    # the path it learned from the upload response. Generous timeout: record
    # duration + 5s for upload + processing.
    #
    # Fallback: ESP32 sometimes drops its WS right after a big HTTPS upload
    # (TLS contention) — the audio_ready event gets lost on the wire. If the
    # event never arrives, scan CAPTURE_DIR for any rec_*.wav newer than when
    # we started listening and use the newest one. The upload itself succeeds
    # even when the post-event fails, so the file is reliably on disk.
    listen_started = time.time()
    full: Path | None = None
    try:
        event = await _await_event("audio_ready", timeout=duration_ms / 1000.0 + 10)
        rel_path = event.get("path", "")
        candidate = (CAPTURE_DIR / os.path.basename(rel_path)).resolve()
        if (str(candidate).startswith(str(CAPTURE_DIR.resolve())) and candidate.exists()):
            full = candidate
    except asyncio.TimeoutError:
        logger.info("audio_ready event missing — falling back to filesystem scan")

    if full is None:
        # Pick the most recent rec_*.wav uploaded since we started waiting.
        candidates = [
            p for p in CAPTURE_DIR.glob("rec_*.wav")
            if p.stat().st_mtime >= listen_started - 0.5
        ]
        if not candidates:
            return _err(f"no recording uploaded within {duration_ms / 1000.0 + 10}s "
                        "(device may have stayed silent, or upload failed)")
        full = max(candidates, key=lambda p: p.stat().st_mtime)
        logger.info("listen fallback: picked %s", full)

    try:
        result = transcribe(full, lang_hint=lang or None)
    except STTError as e:
        return _err(f"STT failed: {e}")
    text = result.get("text", "")
    detected = result.get("language_code", "?")
    if text:
        return f'👂 ({duration_ms}ms, {detected}): "{text}"'
    return f"🎤 Got {full.stat().st_size}-byte recording but STT returned empty"


# ── Tools: motion (intent-level macros + raw move primitive) ────────────────


@mcp.tool()
async def stackchan_move(x: float = 0, y: float = 0, speed: int = 50) -> str:
    """Move Stack-chan's head to an arbitrary angle.

    Primitive — for any "look in direction X" need that isn't covered by
    nod/shake/home macros. LLM should reach for this when a specific angle
    matters, not for ad-hoc nodding (use stackchan_nod for that).

    x: yaw degrees, -128 (left) .. 128 (right), 0 = center
    y: pitch degrees, 0 (level) .. 90 (up)
    speed: 0..100, higher = faster (default 50)
    """
    x = max(-128.0, min(128.0, float(x)))
    y = max(0.0, min(90.0, float(y)))
    speed = max(0, min(100, int(speed)))
    try:
        await link.request("move", {"x": x, "y": y, "speed": speed})
    except (DeviceOffline, DeviceError) as e:
        return _err(str(e))
    return f"🤖 Head -> x={x:.0f}° y={y:.0f}° (speed {speed})"


@mcp.tool()
async def stackchan_nod() -> str:
    """Nod yes. Quick pitch up-down-center sequence handled on-device."""
    try:
        await link.request("nod")
    except (DeviceOffline, DeviceError) as e:
        return _err(str(e))
    return "🤖 *nods*"


@mcp.tool()
async def stackchan_shake() -> str:
    """Shake no. Quick yaw left-right-center sequence handled on-device."""
    try:
        await link.request("shake")
    except (DeviceOffline, DeviceError) as e:
        return _err(str(e))
    return "🤖 *shakes head*"


@mcp.tool()
async def stackchan_home() -> str:
    """Return head to center (yaw=0, pitch=0)."""
    try:
        await link.request("home")
    except (DeviceOffline, DeviceError) as e:
        return _err(str(e))
    return "🤖 Head -> home"


# ── Tools: face (m5stack-avatar procedural expressions) ─────────────────────

# These are the expressions the firmware's m5stack-avatar build exposes via
# its Expression enum. If we add custom expressions later (custom eye/mouth
# shapes), extend this list AND the firmware's face_service.cpp mapping.
_FACE_EXPRESSIONS = ["neutral", "happy", "sad", "angry", "sleepy", "doubt"]


@mcp.tool()
async def stackchan_face(expression: str = "neutral") -> str:
    f"""Change Stack-chan's expression. m5stack-avatar procedural face.

    expression: one of {_FACE_EXPRESSIONS}. Default 'neutral' is the resting
    Stack-chan look (eyes open, mouth straight). The face animates between
    states — no need to call this every frame.
    """
    if expression not in _FACE_EXPRESSIONS:
        return _err(f"unknown expression {expression!r}; choose from {_FACE_EXPRESSIONS}")
    try:
        await link.request("face", {"expression": expression})
    except (DeviceOffline, DeviceError) as e:
        return _err(str(e))
    return f"🙂 Face -> {expression}"


# ── Tools: camera ───────────────────────────────────────────────────────────


@mcp.tool()
async def stackchan_see() -> list:
    """Take a photo through Stack-chan's camera. Returns the image inline so
    the LLM can see it directly.

    GC0308 sensor, fixed-focus ~50cm. Things closer than that will blur."""
    if not link.online:
        return [_err("Stack-chan offline (no WS connection)")]

    # Same robust pattern as stackchan_listen: best-effort RPC, then prefer
    # the photo_ready event but fall back to scanning CAPTURE_DIR for any
    # capture_*.jpg uploaded since we started. ESP32 may drop WS right
    # after the HTTPS upload (TLS contention), losing the event.
    snap_started = time.time()
    try:
        await link.request("snapshot")
    except DeviceError as e:
        logger.info("snapshot RPC didn't ack (%s) — proceeding to wait for upload", e)

    full: Path | None = None
    try:
        event = await _await_event("photo_ready", timeout=10.0)
        rel_path = event.get("path", "")
        candidate = (CAPTURE_DIR / os.path.basename(rel_path)).resolve()
        if (str(candidate).startswith(str(CAPTURE_DIR.resolve())) and candidate.exists()):
            full = candidate
    except asyncio.TimeoutError:
        logger.info("photo_ready event missing — falling back to filesystem scan")

    if full is None:
        # The device is often backed up with mic uploads (TLS contention)
        # so the photo upload can lag well past the event timeout. Poll the
        # filesystem for another 20s — total tool wall time then capped at
        # ~30s worst case, which still beats giving up.
        deadline = time.time() + 20
        while time.time() < deadline:
            candidates = [
                p for p in CAPTURE_DIR.glob("capture_*.jpg")
                if p.stat().st_mtime >= snap_started - 0.5
            ]
            if candidates:
                full = max(candidates, key=lambda p: p.stat().st_mtime)
                logger.info("see fallback (poll): picked %s", full)
                break
            await asyncio.sleep(1)
        if full is None:
            return [_err("no photo uploaded within 30s (device too busy or camera failed)")]

    jpeg = full.read_bytes()
    return [
        Image(data=jpeg, format="jpeg"),
        f"📷 {len(jpeg)} bytes, saved to {full}",
    ]


# ── Tools: LEDs (4, ported from old body-mcp) ───────────────────────────────
# migratorywhale's gateway didn't expose LEDs — their target hardware may not
# have had any. Panice's StackChan kit has 12 RGB LEDs around the front edge,
# and the old project memory's tool inventory included these. Keeping them.


def _validate_color(color: str) -> str | None:
    """Return None if color is a valid #rrggbb or 'name', else error string."""
    if color.startswith("#") and len(color) == 7:
        try:
            int(color[1:], 16)
            return None
        except ValueError:
            pass
    # Common color names device should accept. Keep the device's accepted
    # set as the source of truth; if it rejects, we surface that error.
    if color.lower() in {"red", "green", "blue", "white", "black", "yellow", "cyan", "magenta", "off"}:
        return None
    return f"color must be #rrggbb or name; got {color!r}"


@mcp.tool()
async def stackchan_set_led(index: int, color: str) -> str:
    """Set a single LED.

    index: 0..11 (12 LEDs around the front)
    color: '#rrggbb' or name ('red', 'off', ...)
    """
    if not 0 <= index <= 11:
        return _err(f"index must be 0..11; got {index}")
    if err := _validate_color(color):
        return _err(err)
    try:
        await link.request("led_one", {"index": index, "color": color})
    except (DeviceOffline, DeviceError) as e:
        return _err(str(e))
    return f"💡 LED {index} -> {color}"


@mcp.tool()
async def stackchan_set_all_leds(color: str) -> str:
    """Set all 12 LEDs to the same color."""
    if err := _validate_color(color):
        return _err(err)
    try:
        await link.request("led_all", {"color": color})
    except (DeviceOffline, DeviceError) as e:
        return _err(str(e))
    return f"💡 All LEDs -> {color}"


@mcp.tool()
async def stackchan_set_leds(colors: list[str]) -> str:
    """Set every LED individually. `colors` must be a 12-element list."""
    if len(colors) != 12:
        return _err(f"need exactly 12 colors; got {len(colors)}")
    for i, c in enumerate(colors):
        if err := _validate_color(c):
            return _err(f"colors[{i}]: {err}")
    try:
        await link.request("led_multi", {"colors": colors})
    except (DeviceOffline, DeviceError) as e:
        return _err(str(e))
    return f"💡 Set 12 LEDs"


@mcp.tool()
async def stackchan_clear_leds() -> str:
    """Turn off all LEDs."""
    try:
        await link.request("led_clear")
    except (DeviceOffline, DeviceError) as e:
        return _err(str(e))
    return "💡 LEDs cleared"


# ── Tools: status ───────────────────────────────────────────────────────────


@mcp.tool()
async def stackchan_status() -> str:
    """Check whether Stack-chan is online (WS connected) and quick health."""
    if not link.online:
        return "❌ Stack-chan offline (no WS connection)"
    try:
        info = await link.request("status")
    except (DeviceOffline, DeviceError) as e:
        return _err(str(e))
    return f"✅ Online | {info}"


# ── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    """Dispatch on transport flag: stdio (default) or streamable-http.

    Why the streamable-http branch is hand-rolled: FastMCP 1.x's
    streamable_http_app() hard-codes its Starlette `lifespan` to
    `self.session_manager.run()` (see mcp/server/fastmcp/server.py line ~1044),
    silently dropping any lifespan we pass to FastMCP(). To get our WS + media
    servers started alongside the MCP transport in HTTP mode, we mount
    FastMCP's app inside our own Starlette and put a combined lifespan on the
    outer app. Stdio mode is unaffected — the lowlevel server honors our
    lifespan there, so `mcp.run("stdio")` works as-is.
    """
    if "--http" in sys.argv:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Mount

        import oauth  # local module — OAuth 2.1 endpoints for claude.ai mobile

        port = int(os.environ.get("MCP_PORT", 8767))

        @asynccontextmanager
        async def combined_lifespan(_app):
            # _bringup binds WS + media (process-scoped). Then drive the MCP
            # session_manager — that's the lifespan FastMCP would install
            # itself if we let it own the app, but since we're mounting it
            # inside our own Starlette we run it manually here.
            async with _bringup(None):
                async with mcp.session_manager.run():
                    logger.info(
                        "Gateway up: WS:%d, media:%d, MCP:%d, public=%s",
                        WS_PORT, MEDIA_PORT, port, PUBLIC_BASE_URL,
                    )
                    yield

        # Bearer auth for /mcp. Accepts either:
        #   (1) static MCP_TOKEN bearer — for explicit configs (Desktop's
        #       mcp-remote, Code's ~/.claude.json). The legacy path.
        #   (2) OAuth-issued JWT — for claude.ai mobile app via Integrations.
        #       The JWT is verified statelessly (HS256 + OAUTH_JWT_SECRET).
        #
        # /oauth/* and /.well-known/* are PUBLIC (clients can't reach them
        # otherwise — chicken/egg). Everything else under /mcp goes through
        # bearer.
        #
        # Raw ASGI rather than BaseHTTPMiddleware: streamable-http chunks
        # responses for long tool calls; BaseHTTPMiddleware buffers them
        # and stalls the client until the call completes. Already debugged
        # this once during initial deploy.
        OAUTH_EXEMPT_PREFIXES = (b"/oauth/", b"/.well-known/")

        def bearer_middleware(asgi_app):
            async def middleware(scope, receive, send):
                if scope["type"] != "http":
                    await asgi_app(scope, receive, send)
                    return
                path = scope.get("path", "").encode("latin-1", "replace")
                # Exempt OAuth discovery + endpoints from auth.
                if any(path.startswith(p) for p in OAUTH_EXEMPT_PREFIXES):
                    await asgi_app(scope, receive, send)
                    return
                static_expected = os.environ.get("MCP_TOKEN", "")
                headers = dict(scope.get("headers", []))
                got = headers.get(b"authorization", b"").decode("latin-1", "replace")
                bearer = got[len("Bearer "):] if got.startswith("Bearer ") else ""
                authorized = False
                if static_expected and bearer == static_expected:
                    authorized = True
                elif bearer and oauth.verify_access_token(bearer):
                    authorized = True

                if not authorized:
                    # Per MCP auth spec, WWW-Authenticate points clients at
                    # the protected-resource metadata so they can discover
                    # the auth server and start the OAuth flow.
                    iss = os.environ.get("PUBLIC_BASE_URL", "https://body.aerogelovepanice.com").rstrip("/")
                    metadata_url = f"{iss}/.well-known/oauth-protected-resource"
                    www_auth = f'Bearer resource_metadata="{metadata_url}"'.encode("latin-1")
                    await send({
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", www_auth),
                        ],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": b'{"error":"Unauthorized"}',
                    })
                    return
                await asgi_app(scope, receive, send)
            return middleware

        inner = Starlette(
            routes=oauth.routes() + [Mount("/", app=mcp.streamable_http_app())],
            lifespan=combined_lifespan,
        )
        app = bearer_middleware(inner)
        if not os.environ.get("MCP_TOKEN") and not os.environ.get("OAUTH_JWT_SECRET"):
            logger.warning(
                "MCP_TOKEN and OAUTH_JWT_SECRET both unset — MCP endpoint is "
                "effectively OPEN. Set at least one in .env before exposing."
            )
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    else:
        logger.info("MCP stdio mode (lifespan starts WS:%d, media:%d)",
                    WS_PORT, MEDIA_PORT)
        # stdio mode: install _bringup as the FastMCP lifespan now. (Couldn't
        # do this at module-level FastMCP() construction because http mode
        # also imports this module and would double-run _bringup.)
        mcp.settings.lifespan = _bringup  # type: ignore[attr-defined]
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
