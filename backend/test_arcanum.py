"""
test_arcanum.py -- Arcanum (Phase II) unit tests.
Tests 1-6 require no API key. Test 7 makes a live Gemini Pro call.

Run from: backend/
  python test_arcanum.py
"""

import asyncio
import json
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture — a realistic two-pair session trace
# ─────────────────────────────────────────────────────────────────────────────

FAKE_TRACE = {
    "session_id": "arcana_test_001",
    "system_a_url": "https://saucedemo.com/checkout",
    "system_b_url": "http://localhost:8000/mock/system_b_arca_form.html",
    "mapping_pairs": [
        {
            "pair_id": 1,
            "action": "paste",
            "copied_value": "150.50",
            "source_context": {
                "system": "system_a",
                "url": "https://saucedemo.com/checkout",
                "id": "txt-precio",
                "name": "Precio_Unitario",
                "placeholder": "0.00",
                "label": "Precio del producto",
                "nearby_labels": ["Subtotal", "Cantidad", "Precio del producto"],
            },
            "destination_context": {
                "system": "system_b",
                "url": "http://localhost:8000/mock/system_b_arca_form.html",
                "id": "input-unit-cost",
                "name": "UnitCost",
                "placeholder": "Enter cost...",
                "label": "Unit Cost",
                "nearby_labels": ["Item Cost", "Tax", "Quantity"],
            },
        },
        {
            "pair_id": 2,
            "action": "paste",
            "copied_value": "6",
            "source_context": {
                "system": "system_a",
                "url": "https://saucedemo.com/checkout",
                "id": "txt-qty",
                "name": "Cantidad",
                "placeholder": "0",
                "label": "Cantidad",
                "nearby_labels": ["Total", "Precio del producto"],
            },
            "destination_context": {
                "system": "system_b",
                "url": "http://localhost:8000/mock/system_b_arca_form.html",
                "id": "input-qty",
                "name": "quantity",
                "placeholder": "0",
                "label": "Quantity",
                "nearby_labels": ["Total", "Unit Cost"],
            },
        },
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

print("=" * 60)
print("1. Schema imports — all Pydantic models importable")
print("=" * 60)
from app.arcanum.prompts import (
    FieldSelector, Transformation, MappingRule,
    RuleGraph, RuleGraphResponse,
    build_user_message, SYSTEM_PROMPT,
)
print("  [PASS] All schema classes and helpers imported\n")


print("=" * 60)
print("2. Pydantic schema — valid model construction")
print("=" * 60)
src_sel = FieldSelector(
    primary_selector="name=Precio_Unitario",
    fallback_selectors=["id=txt-precio", "placeholder=0.00"],
    label_hint="Precio del producto",
)
dst_sel = FieldSelector(
    primary_selector="name=UnitCost",
    fallback_selectors=["id=input-unit-cost", "placeholder=Enter cost..."],
    label_hint="Unit Cost",
)
tf = Transformation(
    type="passthrough",
    logic="Copy the decimal value as-is; both fields expect the same monetary format.",
)
rule = MappingRule(
    field_id="rule_01",
    source=src_sel,
    destination=dst_sel,
    transformation=tf,
    confidence=0.95,
    reasoning=(
        "Source label 'Precio del producto' is Spanish for 'Product Price', matching "
        "the destination label 'Unit Cost' semantically. Both fields hold a decimal "
        "monetary value; the human copied '150.50' verbatim with no transformation."
    ),
)
graph = RuleGraph(session_id="arcana_test_001", mappings=[rule])
response = RuleGraphResponse(rule_graph=graph)

assert response.rule_graph.session_id == "arcana_test_001"
assert len(response.rule_graph.mappings) == 1
assert response.rule_graph.mappings[0].confidence == 0.95
print(f"  session_id  : {response.rule_graph.session_id}")
print(f"  rules       : {len(response.rule_graph.mappings)}")
print(f"  confidence  : {response.rule_graph.mappings[0].confidence}")
print("  [PASS]\n")


print("=" * 60)
print("3. Pydantic validation — constraint enforcement")
print("=" * 60)
import pydantic

# confidence must be in [0.0, 1.0]
try:
    bad = MappingRule(
        field_id="bad", source=src_sel, destination=dst_sel, transformation=tf,
        confidence=1.5,   # out of range
        reasoning="test",
    )
    assert False, "Should have raised"
except pydantic.ValidationError as e:
    print(f"  confidence > 1.0 correctly rejected: {e.error_count()} error(s)")

# transformation.type must be one of the four literals
try:
    bad_tf = Transformation(type="magic", logic="do something")
    assert False, "Should have raised"
except pydantic.ValidationError as e:
    print(f"  invalid transform type correctly rejected: {e.error_count()} error(s)")

print("  [PASS]\n")


print("=" * 60)
print("4. JSON round-trip — model_dump_json -> model_validate_json")
print("=" * 60)
json_str = response.model_dump_json(indent=2)
restored = RuleGraphResponse.model_validate_json(json_str)

assert restored.rule_graph.session_id == response.rule_graph.session_id
assert len(restored.rule_graph.mappings) == len(response.rule_graph.mappings)
assert restored.rule_graph.mappings[0].field_id == "rule_01"
assert restored.rule_graph.mappings[0].source.label_hint == "Precio del producto"
assert restored.rule_graph.mappings[0].destination.label_hint == "Unit Cost"
print(f"  Serialised   : {len(json_str)} chars")
print(f"  Round-tripped: session_id={restored.rule_graph.session_id}, rules={len(restored.rule_graph.mappings)}")
print("  [PASS]\n")


print("=" * 60)
print("5. build_user_message() — prompt contains all expected fields")
print("=" * 60)
msg = build_user_message(FAKE_TRACE)

# Session metadata
assert "arcana_test_001"                  in msg, "session_id missing"
assert "saucedemo.com"                    in msg, "system_a_url missing"
assert "system_b_arca_form"               in msg, "system_b_url missing"
assert "CONFIRMED MAPPING PAIRS: 2"       in msg, "pair count missing"

# Pair 1 fields
assert "Precio_Unitario"                  in msg, "source name missing"
assert "Precio del producto"              in msg, "source label missing"
assert "150.50"                           in msg, "copied value missing"
assert "UnitCost"                         in msg, "dest name missing"
assert "Unit Cost"                        in msg, "dest label missing"

# Pair 2 fields
assert "Cantidad"                         in msg, "pair 2 source name missing"
assert "Quantity"                         in msg, "pair 2 dest label missing"
assert '"6"'                              in msg, "pair 2 value missing"

print(f"  Prompt length       : {len(msg)} chars")
print(f"  System prompt length: {len(SYSTEM_PROMPT)} chars")
print(f"  Total to Gemini     : ~{len(SYSTEM_PROMPT) + len(msg)} chars")
print("  [PASS]\n")


print("=" * 60)
print("6. run_learning_phase() — raises ValueError on empty trace (no API call)")
print("=" * 60)
from app.arcanum.brain import run_learning_phase

async def _expect_value_error():
    empty_trace = {
        "session_id": "empty",
        "system_a_url": "http://a",
        "system_b_url": "http://b",
        "mapping_pairs": [],
    }
    broadcasts = []
    try:
        await run_learning_phase(empty_trace, lambda m: broadcasts.append(m) or asyncio.sleep(0))
        assert False, "Expected ValueError"
    except ValueError as e:
        print(f"  ValueError raised correctly: {e}")
    assert len(broadcasts) == 0, "No broadcasts should happen before the raise"

asyncio.run(_expect_value_error())
print("  [PASS]\n")


print("=" * 60)
print("7. run_learning_phase() — live Gemini Pro call (uses API key)")
print("=" * 60)

async def test_live_brain():
    broadcasts: list[dict] = []

    async def capture(msg: dict) -> None:
        broadcasts.append(msg)
        status = msg.get("status") or msg.get("type", "")
        print(f"  [WS] {msg.get('type')} | {status} | {msg.get('message','')[:80]}")

    result = await run_learning_phase(FAKE_TRACE, capture)

    # ── Shape of result ───────────────────────────────────────────────────────
    assert "rule_graph" in result,               "result must have 'rule_graph' key"
    rg = result["rule_graph"]
    assert "session_id" in rg,                   "rule_graph missing session_id"
    assert "mappings"   in rg,                   "rule_graph missing mappings"
    assert isinstance(rg["mappings"], list),     "mappings must be a list"
    assert len(rg["mappings"]) > 0,              "Gemini returned zero rules"

    # ── Each rule has all required fields ─────────────────────────────────────
    for m in rg["mappings"]:
        for key in ("field_id", "source", "destination", "transformation",
                    "confidence", "reasoning"):
            assert key in m, f"Rule missing key: '{key}'"
        assert 0.0 <= m["confidence"] <= 1.0,   "confidence out of [0,1]"
        assert len(m["reasoning"]) > 20,         "reasoning is suspiciously short"
        assert m["transformation"]["type"] in    ("passthrough", "format", "convert", "compute")

    # ── WebSocket events ──────────────────────────────────────────────────────
    phase2_events = [e for e in broadcasts if e.get("type") == "phase_status" and e.get("phase") == 2]
    statuses      = [e["status"] for e in phase2_events]
    assert "reasoning" in statuses, "phase_status 'reasoning' not broadcast"
    assert "complete"  in statuses, "phase_status 'complete' not broadcast"

    # ── session_id preserved ──────────────────────────────────────────────────
    assert rg["session_id"] == "arcana_test_001", \
        f"session_id mismatch: got {rg['session_id']!r}"

    print(f"\n  Rules returned: {len(rg['mappings'])}")
    for m in rg["mappings"]:
        conf  = m["confidence"]
        grade = "HIGH" if conf >= 0.85 else ("MED" if conf >= 0.70 else "LOW")
        src   = m["source"].get("label_hint", "?")
        dst   = m["destination"].get("label_hint", "?")
        print(f"  [{grade} {conf:.2f}] {src} -> {dst}")
        print(f"           {m['reasoning'][:120]}...")
    print()

asyncio.run(test_live_brain())
print("  [PASS]\n")


print("=" * 60)
print("ALL ARCANUM TESTS PASSED")
print("=" * 60)
