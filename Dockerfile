# syntax=docker/dockerfile:1.7
# Claude's body MCP gateway — VPS deployment image.
#
# Runs `python mcp-server/server.py --http`, exposing:
#   - 8765/tcp  device WebSocket (wss://body/ws after Caddy)
#   - 8766/tcp  media side-channel (TTS WAV pull, recording/photo upload)
#   - 8767/tcp  MCP streamable-http transport (for Claude Code/Desktop/browser)
#
# All three sit behind a Caddy reverse-proxy on the host that handles TLS +
# subpath routing. See Caddyfile in the repo root for the deploy-time recipe.

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# ffmpeg: tts.py uses it to transcode ElevenLabs MP3 → 24kHz mono s16 WAV
# (the format firmware/src/playback_service.cpp streams without resampling).
# curl + ca-certificates: install uv. We purge curl after to keep the image
# lean; ffmpeg stays.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates ffmpeg \
    && curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL=/usr/local/bin sh \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency-only layer first so source edits don't re-download deps.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Source: the gateway code + nothing else from the repo (no firmware/, no
# faces/, no tests).
COPY mcp-server ./mcp-server

# Capture dir inside container — mount a host volume here to persist
# recordings/photos across container restarts.
RUN mkdir -p /root/.stackchan/captures /tmp/stackchan_audio

EXPOSE 8765 8766 8767

# `uv run` resolves /opt/venv and execs server.py in HTTP mode.
CMD ["uv", "run", "--frozen", "--no-dev", "python", "mcp-server/server.py", "--http"]
