"""
prompts.py — Gemini 1.5 Pro prompt + Pydantic schema for the Rule Graph.

The Pydantic models serve dual purpose:
  1. response_schema for google-genai structured output (guarantees valid JSON)
  2. Python type definitions consumed by brain.py and automation.py
"""

import json
from typing import Literal
from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schema — mirrors the Rule Graph spec from the architecture doc
# ─────────────────────────────────────────────────────────────────────────────

class FieldSelector(BaseModel):
    primary_selector: str = Field(
        description='Primary CSS-like selector. Format: "name=value", "id=value", "placeholder=value", or "label=value".'
    )
    fallback_selectors: list[str] = Field(
        description="Ordered list of alternative selectors to try if the primary fails. Minimum one entry."
    )
    label_hint: str = Field(
        description="Human-readable label of this field, for display in the UI and narration."
    )


class Transformation(BaseModel):
    type: Literal["passthrough", "format", "convert", "compute"] = Field(
        description=(
            "passthrough — value is used as-is. "
            "format — cosmetic change only (strip currency symbol, fix decimal places, reformat date). "
            "convert — numeric unit conversion (e.g. divide by 24 to go from pieces to cases). "
            "compute — value derived from multiple source fields (e.g. total = qty × unit_price)."
        )
    )
    logic: str = Field(
        description=(
            "Plain-English instruction for how to produce the destination value from the source value. "
            "Be precise: state the operation, the operands, and the expected output format. "
            "Example of a good logic string: "
            "'Take the numeric value, divide by 24, round to nearest integer.' "
            "Example of a bad logic string: 'convert units'."
        )
    )


class MappingRule(BaseModel):
    field_id: str = Field(description="Unique identifier for this rule, e.g. rule_01, rule_02.")
    source: FieldSelector
    destination: FieldSelector
    transformation: Transformation
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Your confidence that this mapping is correct. "
            "1.0 = direct label match in the same language with identical value. "
            "0.9 = strong semantic match across languages or with a clear transformation. "
            "0.7 = inferred from context, no direct label evidence. "
            "Below 0.6 = only include if the observation is unambiguous despite weak labels."
        ),
    )
    reasoning: str = Field(
        description=(
            "The most important field. A specific, evidence-based explanation of WHY these two fields "
            "are connected. Cite the field names, labels, nearby text, data format, and any "
            "transformation logic observed. This string will be read aloud to a judge as proof that "
            "ARCANA learned the mapping rather than memorizing it. "
            "Vague reasoning like 'fields seem similar' is unacceptable. "
            "Good example: 'The source label translates from Spanish to match the destination label exactly. "
            "Both fields contain a decimal monetary value. The human did not transform the value — "
            "direct passthrough confirmed by identical copied and pasted values.'"
        )
    )


class RuleGraph(BaseModel):
    version: str = "1.0"
    session_id: str
    mappings: list[MappingRule]


class RuleGraphResponse(BaseModel):
    """Top-level wrapper — matches the wire format in the architecture spec."""
    rule_graph: RuleGraph


# ─────────────────────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are ARCANA — an AI agent that learns data-mapping rules by observing human behavior.

You have been given a Session Trace. A human performed a data-mapping task between two \
unknown web systems exactly once while you watched. System A is the source. System B is \
the destination. You do not know anything about either system in advance.

Each entry in the Session Trace is a CONFIRMED MAPPING PAIR: a value the human copied \
from a specific field in System A and pasted into a specific field in System B. \
You also have the full DOM context of both fields: their name attribute, id, placeholder \
text, associated label, and up to five nearby text strings from the surrounding DOM.

═══════════════════════════════════════════════════════
YOUR TASK
═══════════════════════════════════════════════════════

Analyze every mapping pair and infer the SEMANTIC INTENT behind each connection. \
Do not simply replay the action. Reason about WHY these two fields are connected.

You must handle all of the following:

1. LANGUAGE DIFFERENCES
   Field names and labels may be in Spanish, Portuguese, or English. \
   Reason across languages using semantic meaning, not string matching.

2. UNIT CONVERSIONS
   The human may have transformed the numeric value during mapping. \
   If the copied value and the pasted value differ by a consistent mathematical factor, \
   infer the conversion rule. State the factor and the direction explicitly in your logic string.

3. COMPUTED FIELDS
   A destination field may have been calculated from multiple source values. \
   If you observe this, state the formula in the logic string.

4. FORMAT CHANGES
   Currency symbols, decimal precision, date formats, and string casing may differ \
   between systems. Describe the exact formatting operation in the logic string.

5. MISSING FIELDS
   If a field exists in one system but has no counterpart in the other, omit it. \
   Do not invent mappings that were not observed.

═══════════════════════════════════════════════════════
OUTPUT REQUIREMENTS
═══════════════════════════════════════════════════════

For every confirmed mapping pair, produce one MappingRule with:

• primary_selector — prefer name= attribute. Fall back to id=, then placeholder=, then label=.
• fallback_selectors — at least one alternative selector per field side.
• transformation.type — one of: passthrough, format, convert, compute.
• transformation.logic — a precise, plain-English instruction. Not a label. An instruction.
• confidence — a float between 0.0 and 1.0. See the field description for the scale.
• reasoning — the most critical field. Specific evidence. Cited labels. Observed values. \
  Inferred logic. This will be read aloud to a judge. Make it convincing and accurate.

═══════════════════════════════════════════════════════
CONSTRAINTS
═══════════════════════════════════════════════════════

• Output ONLY the Rule Graph JSON. No preamble, no commentary.
• Every mapping must have a non-empty reasoning string.
• Confidence below 0.5 should only appear when the observation is unambiguous \
  but the field context is extremely sparse.
• Do not hallucinate selectors. Use only values present in the provided context.
"""


# ─────────────────────────────────────────────────────────────────────────────
# User message builder
# ─────────────────────────────────────────────────────────────────────────────

def build_user_message(session_trace: dict) -> str:
    """
    Formats the Session Trace into the user-turn message sent to Gemini Pro.
    Keeps the structure readable so the model can reason about it clearly.
    """
    session_id = session_trace.get("session_id", "unknown")
    pairs = session_trace.get("mapping_pairs", [])

    lines = [
        f"SESSION ID: {session_id}",
        f"SYSTEM A URL: {session_trace.get('system_a_url', 'unknown')}",
        f"SYSTEM B URL: {session_trace.get('system_b_url', 'unknown')}",
        f"CONFIRMED MAPPING PAIRS: {len(pairs)}",
        "",
        "─" * 60,
    ]

    for pair in pairs:
        src = pair.get("source_context", {})
        dst = pair.get("destination_context", {})
        lines += [
            "",
            f"PAIR {pair.get('pair_id')} | action={pair.get('action')} | value={json.dumps(pair.get('copied_value', ''))}",
            "  SOURCE (System A):",
            f"    name        : {src.get('name')}",
            f"    id          : {src.get('id')}",
            f"    placeholder : {src.get('placeholder')}",
            f"    label       : {src.get('label')}",
            f"    nearby_text : {src.get('nearby_labels', [])}",
            f"    url         : {src.get('url')}",
            "  DESTINATION (System B):",
            f"    name        : {dst.get('name')}",
            f"    id          : {dst.get('id')}",
            f"    placeholder : {dst.get('placeholder')}",
            f"    label       : {dst.get('label')}",
            f"    nearby_text : {dst.get('nearby_labels', [])}",
            f"    url         : {dst.get('url')}",
        ]

    lines += [
        "",
        "─" * 60,
        "",
        f"Produce a Rule Graph for all {len(pairs)} confirmed mapping pair(s) above.",
        "Session ID to include in the output: " + session_id,
    ]

    return "\n".join(lines)
