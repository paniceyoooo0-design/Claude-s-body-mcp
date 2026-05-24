"""ElevenLabs TTS for Stack-chan.

Why ElevenLabs over Fish Audio/edge-tts:
- Panice designed a voice on ElevenLabs Voice Design specifically for stackchan
  (voice_id ObQz2Bok60YT5RSuUln3). Pre-built voices from Azure / OpenAI don't
  match the persona she wants.

Output format: 24kHz mono 16-bit PCM WAV — matches what the firmware playback
service expects (see firmware/src/playback_service.cpp).
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────
ELEVEN_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "ObQz2Bok60YT5RSuUln3")
# eleven_turbo_v2_5 supports zh + en + many others at ~300ms latency.
# eleven_multilingual_v2 is slower but slightly higher quality.
ELEVEN_MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_turbo_v2_5")

# Where TTS WAVs are written. media_server.py serves this directory over HTTP
# so the firmware can pull the audio after we POST it a URL.
AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", "/tmp/stackchan_audio"))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)


class TTSError(RuntimeError):
    """ElevenLabs returned an error or no audio."""


def synthesize(text: str, lang: str = "zh") -> Path:
    """Generate WAV file from text. Returns path inside AUDIO_DIR.

    `lang` is hint-only — ElevenLabs auto-detects language from input.
    We accept it for API symmetry with the MCP tool signature.

    The output file is 24kHz mono s16 PCM WAV — what the firmware playback
    service streams to the speaker without resampling.
    """
    if not ELEVEN_API_KEY:
        raise TTSError(
            "ELEVENLABS_API_KEY not set. Add it to gateway/.env (see "
            "user_credential_habit memory — Panice stores it in her local "
            "credentials file too)."
        )

    # ElevenLabs returns MP3 by default. We ask for pcm_24000 to get raw
    # 24kHz s16 mono samples, then wrap them in a WAV header via ffmpeg.
    # Why not request WAV directly: the ElevenLabs WAV output_format isn't
    # 24kHz mono out of the box, and we want to match firmware expectations
    # exactly to avoid resampling glitches on a memory-tight ESP32.
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",  # We'll convert below; pcm_24000 query param is unreliable
    }
    payload = {
        "text": text,
        "model_id": ELEVEN_MODEL_ID,
        # voice_settings defaults are fine; tweak after first listen.
    }

    t0 = time.time()
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code != 200:
        raise TTSError(f"ElevenLabs HTTP {resp.status_code}: {resp.text[:200]}")
    elapsed = time.time() - t0
    logger.info("TTS %d chars -> %d bytes mp3 in %.2fs", len(text), len(resp.content), elapsed)

    # Save MP3, transcode to 24kHz mono s16 WAV.
    ts = int(time.time() * 1000)
    mp3_path = AUDIO_DIR / f"tts_{ts}.mp3"
    wav_path = AUDIO_DIR / f"tts_{ts}.wav"
    mp3_path.write_bytes(resp.content)

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(mp3_path),
                "-ar", "24000", "-ac", "1", "-sample_fmt", "s16",
                str(wav_path),
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise TTSError(f"ffmpeg failed: {e.stderr.decode('utf-8', 'replace')[:200]}") from e
    finally:
        mp3_path.unlink(missing_ok=True)

    return wav_path
