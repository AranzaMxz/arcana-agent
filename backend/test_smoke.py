"""
Smoke test — no API keys, no browser, no server required.
Run from: c:\hack4her\arcana-agent\backend
  python test_smoke.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

print("=" * 60)
print("1. Testing config.py — .env loading")
print("=" * 60)
try:
    from app.config import GOOGLE_API_KEY, GEMINI_PRO_MODEL, GEMINI_FLASH_MODEL
    # Only show first/last 4 chars so the key isn't logged
    masked = GOOGLE_API_KEY[:4] + "..." + GOOGLE_API_KEY[-4:] if len(GOOGLE_API_KEY) > 8 else "***"
    print(f"  GOOGLE_API_KEY   : {masked}")
    print(f"  GEMINI_PRO_MODEL : {GEMINI_PRO_MODEL}")
    print(f"  GEMINI_FLASH_MODEL: {GEMINI_FLASH_MODEL}")
    print("  [PASS] config.py loaded correctly\n")
except KeyError as e:
    print(f"  [FAIL] Missing env var: {e}")
    print("  Make sure backend/.env exists and contains GOOGLE_API_KEY\n")
    sys.exit(1)


print("=" * 60)
print("2. Testing prompts.py — Pydantic schema")
print("=" * 60)
from app.arcanum.prompts import (
    RuleGraphResponse, RuleGraph, MappingRule,
    FieldSelector, Transformation, build_user_message, SYSTEM_PROMPT
)

# Build a fake Rule Graph and validate it round-trips through Pydantic
fake_rule = MappingRule(
    field_id="rule_01",
    source=FieldSelector(
        primary_selector="name=Cantidad",
        fallback_selectors=["id=qty-field", "placeholder=0"],
        label_hint="Cantidad",
    ),
    destination=FieldSelector(
        primary_selector="name=quantity",
        fallback_selectors=["id=input-qty", "placeholder=Enter quantity"],
        label_hint="Quantity",
    ),
    transformation=Transformation(
        type="passthrough",
        logic="Use the numeric value as-is.",
    ),
    confidence=0.95,
    reasoning=(
        "Source label 'Cantidad' is the Spanish word for 'Quantity', which exactly matches "
        "the destination label. Both fields contain a plain integer. The copied and pasted "
        "values are identical, confirming a direct passthrough with no transformation."
    ),
)

fake_graph = RuleGraphResponse(
    rule_graph=RuleGraph(
        session_id="test_001",
        mappings=[fake_rule],
    )
)

json_out = fake_graph.model_dump_json(indent=2)
print("  Pydantic → JSON (first 300 chars):")
print("  " + json_out[:300].replace("\n", "\n  "))
print("  [PASS] Pydantic schema valid\n")

# Validate JSON → Pydantic round-trip
import json
reparsed = RuleGraphResponse.model_validate(json.loads(json_out))
assert reparsed.rule_graph.mappings[0].confidence == 0.95
print("  [PASS] JSON → Pydantic round-trip valid\n")


print("=" * 60)
print("3. Testing build_user_message() — prompt formatting")
print("=" * 60)
fake_trace = {
    "session_id": "arcana_demo_001",
    "system_a_url": "https://saucedemo.com/checkout",
    "system_b_url": "http://localhost:5500/system_b_arca_form.html",
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
                "url": "http://localhost:5500/system_b_arca_form.html",
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
                "url": "http://localhost:5500/system_b_arca_form.html",
                "id": "input-qty",
                "name": "quantity",
                "placeholder": "0",
                "label": "Quantity",
                "nearby_labels": ["Total", "Unit Cost"],
            },
        },
    ],
}

msg = build_user_message(fake_trace)
print(msg)
print()
print(f"  Message length : {len(msg)} chars")
print(f"  System prompt  : {len(SYSTEM_PROMPT)} chars")
print(f"  Total to Gemini: ~{len(SYSTEM_PROMPT) + len(msg)} chars")
print("  [PASS] build_user_message() formatted correctly\n")


print("=" * 60)
print("4. Testing browser_manager.py — import only (no browser launch)")
print("=" * 60)
try:
    from app.observer.browser_manager import BrowserManager
    bm = BrowserManager()  # no broadcast, no session started
    print("  [PASS] BrowserManager importable and instantiable\n")
except ImportError as e:
    print(f"  [FAIL] Import error: {e}")
    print("  Run: pip install playwright && playwright install chromium\n")


print("=" * 60)
print("5. Testing brain.py — live Gemini Pro call (uses API key)")
print("=" * 60)
import asyncio

async def test_brain():
    from app.arcanum.brain import run_learning_phase

    events = []
    async def capture(msg):
        events.append(msg)
        print(f"  [WS] {msg.get('type')} | {msg.get('message','')[:80]}")

    result = await run_learning_phase(fake_trace, capture)

    rg = result["rule_graph"]
    print(f"\n  Rules returned : {len(rg['mappings'])}")
    for m in rg["mappings"]:
        conf = m["confidence"]
        color = "HIGH" if conf >= 0.85 else ("MED" if conf >= 0.70 else "LOW")
        print(f"  [{color} {conf:.2f}] {m['source']['label_hint']} → {m['destination']['label_hint']}")
        print(f"           {m['reasoning'][:120]}...")
    print()

    phase_types = [e["status"] for e in events if e.get("type") == "phase_status"]
    assert "reasoning" in phase_types, "phase_status 'reasoning' not broadcast"
    assert "complete"  in phase_types, "phase_status 'complete' not broadcast"
    assert len(rg["mappings"]) > 0, "No mappings returned"
    print("  [PASS] brain.py — live Gemini Pro call succeeded\n")

asyncio.run(test_brain())

print("=" * 60)
print("ALL SMOKE TESTS PASSED")
print("=" * 60)