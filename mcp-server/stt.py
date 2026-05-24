"""ElevenLabs Scribe (Speech-to-Text) for Stack-chan.

We use ElevenLabs for STT as well as TTS so the gateway only carries one
external dependency and one API key. Quality is competitive with Whisper-3
for our use case (~5s commands, no real-time streaming need).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

ELEVEN_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
# scribe_v1 is the current STT model. Auto language-detects unless we hint.
ELEVEN_STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v1")


class STTError(RuntimeError):
    pass


def transcribe(wav_path: Path, lang_hint: str | None = None) -> dict:
    """Send a WAV file to ElevenLabs Scribe; return the JSON response.

    Response shape: {"text": "...", "language_code": "...", "language_probability": ...}.
    Returns the raw dict so callers can decide which fields to surface.
    """
    if not ELEVEN_API_KEY:
        raise STTError("ELEVENLABS_API_KEY not set")
    if not wav_path.exists():
        raise STTError(f"audio file not found: {wav_path}")

    url = "https://api.elevenlabs.io/v1/speech-to-text"
    headers = {"xi-api-key": ELEVEN_API_KEY}
    # multipart: file + model_id (+ language_code if hinted)
    data: dict = {"model_id": ELEVEN_STT_MODEL}
    if lang_hint:
        # ISO-639-1 codes — zh, en, ja. Wrong hint reduces accuracy so we
        # only pass it when the caller explicitly asks.
        data["language_code"] = lang_hint

    with open(wav_path, "rb") as f:
        resp = requests.post(
            url,
            headers=headers,
            data=data,
            files={"file": (wav_path.name, f, "audio/wav")},
            timeout=30,
        )
    if resp.status_code != 200:
        raise STTError(f"ElevenLabs STT HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()
