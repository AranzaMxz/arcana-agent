"""
brain.py — Phase II: The Arcanum.

Sends the Session Trace to Gemini 1.5 Pro, receives a validated Rule Graph
via structured output, broadcasts phase_status events throughout.
"""

import json
import logging
from typing import Callable, Awaitable

from google import genai
from google.genai import types

from app.config import GOOGLE_API_KEY, GEMINI_PRO_MODEL
from app.arcanum.prompts import SYSTEM_PROMPT, build_user_message, RuleGraphResponse

logger = logging.getLogger(__name__)

# Module-level client — stateless, safe to reuse across requests.
# DEMO RISK: if GOOGLE_API_KEY is missing from .env this import will raise KeyError.
_client = genai.Client(api_key=GOOGLE_API_KEY)

_GENERATION_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    response_mime_type="application/json",
    response_schema=RuleGraphResponse,
    # Low temperature: we want deterministic, structured JSON, not creative output.
    temperature=0.1,
)


async def run_learning_phase(
    session_trace: dict,
    broadcast: Callable[[dict], Awaitable[None]],
) -> dict:
    """
    Phase II entry point.

    Args:
        session_trace: The confirmed mapping pairs from Phase I.
        broadcast:     Async callable that sends a dict to the WebSocket.

    Returns:
        The Rule Graph as a plain dict (rule_graph key at top level).
    """
    pair_count = len(session_trace.get("mapping_pairs", []))

    if pair_count == 0:
        raise ValueError("Session trace contains no confirmed mapping pairs. Nothing to learn.")

    # ── Broadcast reasoning start — fills the visual gap during API latency ──
    await broadcast({
        "type": "phase_status",
        "phase": 2,
        "status": "reasoning",
        "message": f"ARCANA is analyzing your session — {pair_count} confirmed pair(s)...",
    })

    user_message = build_user_message(session_trace)

    logger.info("Calling Gemini Pro | session=%s | pairs=%d | prompt_chars=%d",
                session_trace.get("session_id"), pair_count, len(user_message))

    try:
        response = await _client.aio.models.generate_content(
            model=GEMINI_PRO_MODEL,
            contents=user_message,
            config=_GENERATION_CONFIG,
        )
    except Exception as exc:
        logger.error("Gemini Pro API call failed: %s", exc)
        await broadcast({
            "type": "phase_status",
            "phase": 2,
            "status": "error",
            "message": f"Gemini API error: {exc}",
        })
        raise

    # ── Parse and validate ────────────────────────────────────────────────────
    raw_text = response.text or ""

    try:
        rule_graph_response = RuleGraphResponse.model_validate_json(raw_text)
    except Exception as exc:
        # The model returned something that didn't match our schema.
        # Log the raw response so we can debug, then re-raise.
        logger.error("Rule Graph parse failed.\nRaw response:\n%s\nError: %s", raw_text, exc)
        await broadcast({
            "type": "phase_status",
            "phase": 2,
            "status": "error",
            "message": "Could not parse Rule Graph from Gemini response.",
        })
        raise ValueError(f"Rule Graph parse error: {exc}") from exc

    # ── Post-process ──────────────────────────────────────────────────────────

    # Ensure session_id is always set (model may omit it)
    if not rule_graph_response.rule_graph.session_id:
        rule_graph_response.rule_graph.session_id = session_trace.get("session_id", "unknown")

    mapping_count = len(rule_graph_response.rule_graph.mappings)
    result = rule_graph_response.model_dump()

    logger.info("Rule Graph built | session=%s | rules=%d",
                rule_graph_response.rule_graph.session_id, mapping_count)

    # ── Broadcast completion with the full rule graph ─────────────────────────
    await broadcast({
        "type": "phase_status",
        "phase": 2,
        "status": "complete",
        "message": f"Rule Graph ready — {mapping_count} mapping rule(s) learned.",
        "rule_graph": result["rule_graph"],
    })

    return result
