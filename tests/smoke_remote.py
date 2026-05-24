"""Live smoke test against the deployed VPS gateway.

Runs the mock device + a real MCP client in the same process, both pointing
at https://body.aerogelovepanice.com. Validates the full chain:

  MCP client -> streamable-http -> Caddy -> gateway -> DeviceLink -> mock device

Skipped tools: stackchan_say (would burn ElevenLabs credits) and listen/see
(timing-fragile). Validates the cheap pure-WS path which is what proves the
transport.

Usage:
    STACKCHAN_TOKEN=... MCP_TOKEN=... uv run python tests/smoke_remote.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mock_device import MockDevice  # noqa: E402

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


BASE = os.environ.get("BASE", "https://body.aerogelovepanice.com")
STACKCHAN_TOKEN = os.environ["STACKCHAN_TOKEN"]
MCP_TOKEN = os.environ["MCP_TOKEN"]


async def main() -> int:
    # Mock device connects via wss and stays online.
    dev = MockDevice(
        ws_url=f"{BASE.replace('https://', 'wss://')}/ws",
        media_url=BASE,
        token=STACKCHAN_TOKEN,
    )
    dev_task = asyncio.create_task(dev.run())
    # Give the device a beat to connect through Caddy.
    await asyncio.sleep(2)

    headers = {"Authorization": f"Bearer {MCP_TOKEN}"}
    try:
        async with streamablehttp_client(f"{BASE}/mcp/", headers=headers) as (
            read, write, _get_session_id,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                print(f"tools available ({len(names)}): {names}")

                # Cheap tools that just round-trip a request/ack — no TTS, no
                # ElevenLabs calls, no real audio.
                for name in ["stackchan_nod", "stackchan_shake", "stackchan_home"]:
                    result = await session.call_tool(name, {})
                    text = result.content[0].text if result.content else "(no content)"
                    print(f"  {name}() -> {text}")

                result = await session.call_tool(
                    "stackchan_move", {"x": 30, "y": 15, "speed": 50}
                )
                print(f"  stackchan_move(30, 15, 50) -> {result.content[0].text}")

                result = await session.call_tool("stackchan_face", {"expression": "happy"})
                print(f"  stackchan_face(happy) -> {result.content[0].text}")

                result = await session.call_tool(
                    "stackchan_set_all_leds", {"color": "#ff8800"}
                )
                print(f"  stackchan_set_all_leds(#ff8800) -> {result.content[0].text}")

                result = await session.call_tool("stackchan_status", {})
                print(f"  stackchan_status() -> {result.content[0].text}")
    finally:
        dev_task.cancel()
        try:
            await dev_task
        except asyncio.CancelledError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
