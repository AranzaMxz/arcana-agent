"""
automation.py — Phase III execution engine.

For each mapping rule in the Rule Graph:
  1. Locate the source field on System A using the selector fallback chain
  2. Read its current value
  3. Apply the transformation via Gemini Flash (non-hardcoded)
  4. Locate the destination field on System B using the selector fallback chain
  5. Write the transformed value
  6. Broadcast the step result + trigger narration

Selector fallbacks NEVER throw — failing to find a selector logs a warning
and moves to the next fallback. Failing all selectors skips the rule with a
broadcast warning so execution continues for the remaining fields.
"""

import logging
from typing import Callable, Awaitable

from playwright.async_api import Page, Locator
from google import genai
from google.genai import types

from app.config import GOOGLE_API_KEY, GEMINI_FLASH_TEXT_MODEL
from app.execution.narrator import narrate_step

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=GOOGLE_API_KEY)


# ─────────────────────────────────────────────────────────────────────────────
# Selector resolution
# ─────────────────────────────────────────────────────────────────────────────

def _parse_selector(raw: str) -> tuple[str, str]:
    """Split "type=value" into (type, value). Falls back to ("raw", raw)."""
    if "=" in raw:
        sel_type, sel_value = raw.split("=", 1)
        return sel_type.strip().lower(), sel_value.strip()
    return "raw", raw


async def _locate_field(page: Page, selectors: list[str]) -> Locator | None:
    """
    Try each selector in order. Returns the first visible match or None.
    Never raises — exceptions per-selector are logged as warnings.
    """
    for raw in selectors:
        if not raw:
            continue
        sel_type, sel_value = _parse_selector(raw)
        try:
            if sel_type == "name":
                loc = page.locator(f'[name="{sel_value}"]')
            elif sel_type == "id":
                # id= can contain characters that break #id CSS shorthand
                loc = page.locator(f'[id="{sel_value}"]')
            elif sel_type == "placeholder":
                loc = page.locator(f'[placeholder="{sel_value}"]')
            elif sel_type == "label":
                # get_by_label uses accessible name — most reliable cross-framework
                loc = page.get_by_label(sel_value, exact=False)
            else:
                loc = page.locator(raw)

            if await loc.count() > 0:
                return loc.first

        except Exception as exc:
            logger.warning("Selector '%s' failed: %s — trying next", raw, exc)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Value reading
# ─────────────────────────────────────────────────────────────────────────────

async def _read_field_value(locator: Locator) -> str:
    """
    Read a value from a Playwright locator.
    Tries input_value() first (works for <input>, <select>, <textarea>).
    Falls back to inner_text() for display-only elements (<span>, <td>, etc.).
    """
    try:
        val = await locator.input_value()
        if val is not None:
            return val.strip()
    except Exception:
        pass
    try:
        val = await locator.inner_text()
        return (val or "").strip()
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Transformation evaluation (non-hardcoded — handled by Gemini Flash)
# ─────────────────────────────────────────────────────────────────────────────

async def _apply_transformation(value: str, transformation: dict) -> str:
    """
    Apply a Rule Graph transformation to a value.

    passthrough → returned as-is (no API call, no latency).
    format / convert / compute → Gemini Flash evaluates the logic string.

    Falls back to the original value if the API call fails, so execution
    always continues regardless of transformation errors.
    """
    t_type = transformation.get("type", "passthrough")
    logic  = (transformation.get("logic") or "").strip()

    if t_type == "passthrough" or not logic:
        return value

    prompt = (
        f"Apply the following transformation to the input value.\n"
        f"Return ONLY the transformed result — no explanation, no labels, no punctuation around it.\n\n"
        f"Transformation: {logic}\n"
        f"Input value: {value}\n"
        f"Result:"
    )

    try:
        response = await _client.aio.models.generate_content(
            model=GEMINI_FLASH_TEXT_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=64,
            ),
        )
        result = (response.text or "").strip()
        # DEMO RISK: model occasionally wraps result in quotes or adds a period.
        # Strip common wrapper characters defensively.
        result = result.strip('"\'`.,')
        return result if result else value
    except Exception as exc:
        logger.warning("Transformation eval failed (%s) — using raw value '%s'", exc, value)
        return value


# ─────────────────────────────────────────────────────────────────────────────
# Execution engine
# ─────────────────────────────────────────────────────────────────────────────

class AutomationEngine:
    """
    Executes a Rule Graph against live Playwright pages.
    Pages are injected so main.py can reuse the observation session's browser.
    """

    def __init__(
        self,
        page_a: Page,
        page_b: Page,
        broadcast: Callable[[dict], Awaitable[None]],
        session_id: str,
    ):
        self.page_a    = page_a
        self.page_b    = page_b
        self.broadcast = broadcast
        self.session_id = session_id

    async def execute(
        self,
        rule_graph: dict,
        url_a: str | None = None,
        url_b: str | None = None,
    ) -> list[dict]:
        """
        Run all mapping rules in the Rule Graph.

        url_a / url_b: if provided, navigate the pages before executing.
        Returns a list of per-rule result dicts for the execution log.
        """
        await self.broadcast({
            "type": "phase_status",
            "phase": 3,
            "status": "executing",
            "message": "ARCANA is executing the mapping autonomously...",
        })

        if url_a:
            await self.page_a.goto(url_a, wait_until="domcontentloaded")
        if url_b:
            await self.page_b.goto(url_b, wait_until="domcontentloaded")

        mappings = rule_graph.get("mappings", [])
        results  = []

        for rule in mappings:
            result = await self._execute_rule(rule)
            results.append(result)

        done_count    = sum(1 for r in results if r["status"] == "done")
        skipped_count = len(results) - done_count

        await self.broadcast({
            "type": "phase_status",
            "phase": 3,
            "status": "complete",
            "message": (
                f"Execution complete — {done_count} field(s) filled"
                + (f", {skipped_count} skipped." if skipped_count else ".")
            ),
        })

        return results

    # ── Single rule ──────────────────────────────────────────────────────────

    async def _execute_rule(self, rule: dict) -> dict:
        field_id     = rule.get("field_id", "?")
        source       = rule.get("source", {})
        destination  = rule.get("destination", {})
        transformation = rule.get("transformation", {})
        confidence   = float(rule.get("confidence", 0.0))
        reasoning    = rule.get("reasoning", "")

        source_label = source.get("label_hint") or field_id
        dest_label   = destination.get("label_hint") or field_id

        src_selectors = [source.get("primary_selector")] + list(source.get("fallback_selectors") or [])
        dst_selectors = [destination.get("primary_selector")] + list(destination.get("fallback_selectors") or [])

        # ── Read ─────────────────────────────────────────────────────────────
        await self.broadcast({
            "type": "execution_step",
            "status": "reading",
            "field_id": field_id,
            "source_label": source_label,
            "dest_label": dest_label,
            "message": f"Reading '{source_label}' from System A...",
        })

        src_el = await _locate_field(self.page_a, src_selectors)
        if src_el is None:
            return await self._skip(field_id, source_label, dest_label, "source field not found on System A")

        value_read = await _read_field_value(src_el)

        # ── Transform ────────────────────────────────────────────────────────
        value_written = await _apply_transformation(value_read, transformation)

        # ── Write ────────────────────────────────────────────────────────────
        await self.broadcast({
            "type": "execution_step",
            "status": "writing",
            "field_id": field_id,
            "source_label": source_label,
            "dest_label": dest_label,
            "value_read": value_read,
            "value_written": value_written,
            "confidence": confidence,
            "message": f"Writing '{value_written}' into '{dest_label}' on System B...",
        })

        dst_el = await _locate_field(self.page_b, dst_selectors)
        if dst_el is None:
            return await self._skip(field_id, source_label, dest_label, "destination field not found on System B")

        # fill() in Playwright clears then types, which works for most inputs.
        # DEMO RISK: React/Vue controlled inputs may need triple-click + type instead.
        await dst_el.fill(value_written)

        # ── Done ─────────────────────────────────────────────────────────────
        step_context = {
            "field_id":           field_id,
            "source_label":       source_label,
            "dest_label":         dest_label,
            "source_name":        source.get("primary_selector", ""),
            "dest_name":          destination.get("primary_selector", ""),
            "value_read":         value_read,
            "value_written":      value_written,
            "transformation_type": transformation.get("type", "passthrough"),
            "reasoning":          reasoning,
            "confidence":         confidence,
        }

        await self.broadcast({
            "type": "execution_step",
            "status": "done",
            **step_context,
            "message": f"✓ '{source_label}' → '{dest_label}': {value_read!r} → {value_written!r}",
        })

        # Narrate after broadcasting so the UI step card appears before audio plays
        await narrate_step(step_context, self.broadcast)

        return {**step_context, "status": "done"}

    async def _skip(self, field_id: str, source_label: str, dest_label: str, reason: str) -> dict:
        logger.warning("Skipping rule '%s': %s", field_id, reason)
        await self.broadcast({
            "type": "execution_step",
            "status": "skipped",
            "field_id": field_id,
            "source_label": source_label,
            "dest_label": dest_label,
            "reason": reason,
            "message": f"⚠ Skipped '{source_label}' → '{dest_label}': {reason}",
        })
        return {
            "field_id": field_id,
            "status": "skipped",
            "source_label": source_label,
            "dest_label": dest_label,
            "reason": reason,
        }
