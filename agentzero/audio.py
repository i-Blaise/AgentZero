"""
Voice transcription via OpenAI Whisper.
Always uses OpenAI regardless of LLM_PROVIDER — Anthropic has no transcription API.
"""
from __future__ import annotations

import openai

from agentzero.config import OPENAI_API_KEY

_client: openai.AsyncOpenAI | None = None


def _get_client() -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        _client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _client


async def transcribe(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    resp = await _get_client().audio.transcriptions.create(
        model="whisper-1",
        file=(filename, audio_bytes, "audio/ogg"),
    )
    return resp.text.strip()
