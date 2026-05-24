"""End-to-end test: gateway + mock device, all 13 tools.

Runs everything in one asyncio loop:
- DeviceLink WS server on a random high port
- media_server HTTP server on a random high port
- MockDevice connecting from the same process
- Calls each tool's underlying request directly (skips FastMCP layer — that's
  just a wrapper, the semantics live in our link.request + media_server)

What this catches:
- WS auth (bearer token mismatch)
- Request/response routing (id matching, timeout, error propagation)
- Event delivery (audio_ready / photo_ready)
- Media uploads end-to-end
- TTS WAV is served over HTTP (mock device fetches it like real firmware would)

What this does NOT catch (intentional):
- Real ElevenLabs TTS/STT calls — those hit network and need API key. Tested
  separately when wiring up VPS .env.
- FastMCP transport bugs — that's framework code we trust.
- Firmware quirks — that's the next phase.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest

# Path setup so we can import mcp-server/* as plain modules
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp-server"))
sys.path.insert(0, str(Path(__file__).parent))

from device_link import DeviceError, DeviceLink, DeviceOffline  # noqa: E402
from media_server import AUDIO_DIR, CAPTURE_DIR, run_in_loop as run_media_server  # noqa: E402
from mock_device import MockDevice, _make_silent_wav  # noqa: E402

# Token only used inside fixtures — set/restored around each test so we don't
# pollute global env that other test files (test_device_link.py) rely on being
# clean. device_link.py and media_server.py both read STACKCHAN_TOKEN at
# request time, so fixture-scope mutation works.
TEST_TOKEN = "test-token-12345"


def _free_port() -> int:
    """Grab a free port for this test run."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def gateway():
    """Start DeviceLink + media server + connected MockDevice. Yield the link.

    Sets STACKCHAN_TOKEN inside the fixture and restores it on exit so test
    files that assume no-token (e.g. test_device_link.py) aren't affected.
    """
    ws_port = _free_port()
    media_port = _free_port()

    prev_token = os.environ.get("STACKCHAN_TOKEN")
    prev_base = os.environ.get("PUBLIC_BASE_URL")
    os.environ["STACKCHAN_TOKEN"] = TEST_TOKEN
    os.environ["PUBLIC_BASE_URL"] = f"http://127.0.0.1:{media_port}"

    link = DeviceLink()
    await link.start(host="127.0.0.1", port=ws_port)
    media_runner = await run_media_server(host="127.0.0.1", port=media_port)

    dev = MockDevice(
        ws_url=f"ws://127.0.0.1:{ws_port}/",
        media_url=f"http://127.0.0.1:{media_port}",
        token=TEST_TOKEN,
    )
    dev_task = asyncio.create_task(dev.run())

    for _ in range(50):
        if link.online:
            break
        await asyncio.sleep(0.05)
    assert link.online, "mock device failed to connect within 2.5s"

    try:
        yield link, dev, media_port
    finally:
        dev_task.cancel()
        await asyncio.gather(dev_task, return_exceptions=True)
        await media_runner.cleanup()
        await link.stop()
        # Restore env so the next test in the session starts clean.
        if prev_token is None:
            os.environ.pop("STACKCHAN_TOKEN", None)
        else:
            os.environ["STACKCHAN_TOKEN"] = prev_token
        if prev_base is None:
            os.environ.pop("PUBLIC_BASE_URL", None)
        else:
            os.environ["PUBLIC_BASE_URL"] = prev_base


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_rejects_bad_token():
    """A device with a wrong bearer token must be refused at the WS upgrade."""
    prev = os.environ.get("STACKCHAN_TOKEN")
    os.environ["STACKCHAN_TOKEN"] = TEST_TOKEN
    ws_port = _free_port()
    link = DeviceLink()
    await link.start(host="127.0.0.1", port=ws_port)
    try:
        import websockets
        with pytest.raises(Exception):
            async with websockets.connect(
                f"ws://127.0.0.1:{ws_port}/",
                additional_headers={"Authorization": "Bearer wrong"},
            ) as _ws:
                pass
        assert not link.online, "bad-token connection must not appear online"
    finally:
        await link.stop()
        if prev is None:
            os.environ.pop("STACKCHAN_TOKEN", None)
        else:
            os.environ["STACKCHAN_TOKEN"] = prev


@pytest.mark.asyncio
async def test_simple_gestures(gateway):
    """nod / shake / home / move / face / led_* all round-trip an ack."""
    link, dev, _ = gateway

    assert await link.request("nod") == {"gesture": "nod"}
    assert await link.request("shake") == {"gesture": "shake"}
    assert await link.request("home") == {"gesture": "home"}

    r = await link.request("move", {"x": 30, "y": 15, "speed": 50})
    assert r == {"x": 30, "y": 15}

    r = await link.request("face", {"expression": "happy"})
    assert r == {"face": "happy"}

    await link.request("led_one", {"index": 3, "color": "#ff0000"})
    await link.request("led_all", {"color": "blue"})
    await link.request("led_clear")
    # Mock just acks with the method name; that's enough to prove routing.


@pytest.mark.asyncio
async def test_play_fetches_audio(gateway):
    """`play` requires the device be able to HTTP GET the URL we gave it."""
    link, dev, media_port = gateway

    # Drop a fake TTS WAV in AUDIO_DIR so the media server has something to
    # serve. Real synthesize() is mocked out — we're only testing transport.
    wav = AUDIO_DIR / "test_play.wav"
    wav.write_bytes(_make_silent_wav(0.5))

    url = f"http://127.0.0.1:{media_port}/audio/test_play.wav"
    result = await link.request("play", {"voice_url": url})
    assert result == {"played_bytes": "ok"}


@pytest.mark.asyncio
async def test_listen_event_flow(gateway):
    """listen request → upload → audio_ready event → file exists on disk."""
    link, dev, _ = gateway

    # Subscribe before we send the request so we don't miss the event.
    events = link.subscribe_events()
    try:
        await link.request("listen", {"duration_ms": 1000})
        # Mock device will POST then emit. Generous timeout.
        event = await asyncio.wait_for(events.get(), timeout=5)
        assert event["event"] == "audio_ready"
        path = Path(event["path"])
        assert path.exists(), f"upload should have created {path}"
        assert path.stat().st_size > 0
        # File must live in CAPTURE_DIR — defends against the path-traversal
        # check in stackchan_listen.
        assert str(path.resolve()).startswith(str(CAPTURE_DIR.resolve()))
    finally:
        link.unsubscribe_events(events)


@pytest.mark.asyncio
async def test_snapshot_event_flow(gateway):
    """snapshot request → upload → photo_ready event → JPEG on disk."""
    link, dev, _ = gateway

    events = link.subscribe_events()
    try:
        await link.request("snapshot")
        event = await asyncio.wait_for(events.get(), timeout=5)
        assert event["event"] == "photo_ready"
        path = Path(event["path"])
        assert path.exists()
        # Quick sanity: JPEG magic bytes
        assert path.read_bytes()[:2] == b"\xff\xd8"
    finally:
        link.unsubscribe_events(events)


@pytest.mark.asyncio
async def test_device_offline_raises():
    """request() when no device is connected must raise DeviceOffline."""
    link = DeviceLink()
    ws_port = _free_port()
    await link.start(host="127.0.0.1", port=ws_port)
    try:
        with pytest.raises(DeviceOffline):
            await link.request("nod")
    finally:
        await link.stop()


@pytest.mark.asyncio
async def test_unknown_method_propagates_error(gateway):
    """Mock acks unknown methods with ok:false; should raise DeviceError."""
    link, _, _ = gateway
    with pytest.raises(DeviceError, match="unknown method"):
        await link.request("nonsense_method")
