"""
narrator.py — Phase III real-time narration in ARCANA's voice.

Primary path : gemini-2.5-flash-preview-tts  → spoken audio sent as base64 PCM
Fallback path: gemini-3.1-flash-lite          → text narration when TTS quota exhausted

The two-agent design is intentional:
  - TTS agent is specialized and impressive during the demo's climactic execution phase
  - Text agent is high-quota (500 RPD) so narration never goes completely silent
"""

import base64
import logging
from typing import Callable, Awaitable

from google import genai
from google.genai import types

from app.config import GOOGLE_API_KEY, GEMINI_FLASH_MODEL, GEMINI_FLASH_TEXT_MODEL

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=GOOGLE_API_KEY)

# Kore: confident, clear, slightly formal — matches ARCANA's voice spec
_ARCANA_VOICE = "Kore"

# PCM audio spec returned by Gemini TTS — sent to frontend so it can decode correctly
_AUDIO_SAMPLE_RATE = 24000
_AUDIO_CHANNELS = 1
_AUDIO_BIT_DEPTH = 16

_NARRATION_SYSTEM_PROMPT = """\
You are ARCANA — an AI agent narrating your own actions in real time during a live demo.
Your voice is confident, analytical, and occasionally witty. Never robotic, never verbose.
Speak in first person. Generate exactly ONE or TWO sentences.
Be specific: name the fields, state the transformation if one occurred.
Do not use filler openers like "I am now processing" or "Please wait".
Speak as if briefing a sharp colleague who values precision over pleasantries.
"""


def _build_narration_prompt(step: dict) -> str:
    """Builds the generation prompt from an execution step context dict."""
    src = step.get("source_label") or step.get("source_name") or "source field"
    dst = step.get("dest_label") or step.get("dest_name") or "destination field"
    value_read    = step.get("value_read", "")
    value_written = step.get("value_written", "")
    t_type        = step.get("transformation_type", "passthrough")
    reasoning     = step.get("reasoning", "")
    confidence    = float(step.get("confidence", 1.0))

    parts = [
        f"You just completed a field mapping:",
        f"  Source field   : {src}",
        f"  Destination    : {dst}",
        f"  Value read     : {value_read}",
        f"  Value written  : {value_written}",
        f"  Transformation : {t_type}",
        f"  Confidence     : {confidence:.0%}",
    ]
    if reasoning:
        parts.append(f"  Why you did it : {reasoning}")

    parts.append("")
    parts.append("Narrate this in 1-2 sentences in ARCANA's voice.")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

async def narrate_step(
    step: dict,
    broadcast: Callable[[dict], Awaitable[None]],
) -> None:
    """
    Generate narration for one execution step and broadcast it over WebSocket.

    Args:
        step:      Execution step context dict (source/dest labels, values, rule info).
        broadcast: Async callable wired to the session's WebSocket.
    """
    prompt = _build_narration_prompt(step)

    try:
        await _narrate_tts(prompt, step, broadcast)
        return
    except Exception as exc:
        exc_str = str(exc)
        if "429" in exc_str or "RESOURCE_EXHAUSTED" in exc_str:
            logger.warning("TTS quota exhausted — falling back to text narration")
        else:
            # DEMO RISK: unexpected TTS failure — log and continue with text fallback
            logger.error("TTS narration failed (%s) — falling back to text", exc_str[:120])

    try:
        await _narrate_text(prompt, step, broadcast)
    except Exception as exc:
        # Last resort: silent failure with a minimal canned line so the UI isn't blank
        logger.error("Text narration also failed: %s", exc)
        await broadcast({
            "type": "narration",
            "mode": "text",
            "text": f"Mapped \"{step.get('source_label', '?')}\" → \"{step.get('dest_label', '?')}\".",
            "step": step,
        })


# ─────────────────────────────────────────────────────────────────────────────
# TTS path
# ─────────────────────────────────────────────────────────────────────────────

async def _narrate_tts(
    prompt: str,
    step: dict,
    broadcast: Callable[[dict], Awaitable[None]],
) -> None:
    # Step 1: Generate the narration TEXT with the text model.
    # TTS models are converters (text-in → audio-out), not generators —
    # passing system_instruction to them causes a 500 INTERNAL server error.
    text_response = await _client.aio.models.generate_content(
        model=GEMINI_FLASH_TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_NARRATION_SYSTEM_PROMPT,
            temperature=0.7,
        ),
    )
    narration_text = (text_response.text or "").strip()
    if not narration_text:
        raise ValueError("Text generation step returned empty narration")

    # Step 2: Convert the generated text to speech.
    # No system_instruction — TTS model receives only the text to speak.
    audio_response = await _client.aio.models.generate_content(
        model=GEMINI_FLASH_MODEL,
        contents=narration_text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=_ARCANA_VOICE,
                    )
                )
            ),
        ),
    )

    candidates = audio_response.candidates or []
    if not candidates:
        raise ValueError("TTS response contained no candidates")

    parts = candidates[0].content.parts or []
    audio_part = next((p for p in parts if p.inline_data is not None), None)
    if audio_part is None:
        raise ValueError("TTS response contained no audio inline_data")

    raw = audio_part.inline_data.data
    mime = audio_part.inline_data.mime_type or "audio/pcm"

    if isinstance(raw, (bytes, bytearray)):
        audio_b64 = base64.b64encode(raw).decode("utf-8")
    else:
        audio_b64 = raw  # some SDK versions already return base64

    await broadcast({
        "type": "narration",
        "mode": "audio",
        "audio_b64": audio_b64,
        "mime_type": mime,
        "text": narration_text,       # caption shown in UI while audio plays
        "sample_rate": _AUDIO_SAMPLE_RATE,
        "channels": _AUDIO_CHANNELS,
        "bit_depth": _AUDIO_BIT_DEPTH,
        "step": step,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Text fallback path
# ─────────────────────────────────────────────────────────────────────────────

async def _narrate_text(
    prompt: str,
    step: dict,
    broadcast: Callable[[dict], Awaitable[None]],
) -> None:
    response = await _client.aio.models.generate_content(
        model=GEMINI_FLASH_TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_NARRATION_SYSTEM_PROMPT,
            temperature=0.7,
        ),
    )

    text = (response.text or "").strip()
    if not text:
        raise ValueError("Text narration returned empty response")

    await broadcast({
        "type": "narration",
        "mode": "text",
        "text": text,
        "step": step,
    })
