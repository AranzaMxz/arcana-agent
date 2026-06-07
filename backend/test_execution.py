"""
test_execution.py -- Phase III execution engine tests.
Tests 1-7: no API key needed (mock Playwright pages, narration mocked).
Tests 8-9: live Gemini Flash calls (API key required).

Run from: backend/
  python test_execution.py
"""

import asyncio
import sys
import os
from unittest.mock import patch, AsyncMock

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))


# ─────────────────────────────────────────────────────────────────────────────
# Mock Playwright page / locator
# ─────────────────────────────────────────────────────────────────────────────

class MockLocator:
    """
    Simulates a Playwright Locator.
    found=False  -> count() returns 0 (field not on page).
    filled_with  -> set by fill(); observable after execution.
    """
    def __init__(self, value=None, found=True):
        self._value = value
        self._found = found
        self.first  = self          # _locate_field returns loc.first
        self.filled_with = None

    async def count(self):       return 1 if self._found else 0
    async def input_value(self):
        if self._value is not None: return self._value
        raise Exception("not an input element")
    async def inner_text(self):  return self._value or ""
    async def fill(self, v):     self.filled_with = v


class MockPage:
    """
    Simulates a Playwright Page.
    field_map maps selector substrings (e.g. "UnitCost") to field values.
    Locators are cached per key so fills are observable afterwards.
    """
    def __init__(self, field_map=None):
        self._field_map   = field_map or {}
        self._locators    = {}      # key -> MockLocator (cached)
        self.navigated_to = []

    def locator(self, selector):
        for key, val in self._field_map.items():
            if key in selector:
                if key not in self._locators:
                    self._locators[key] = MockLocator(val, found=True)
                return self._locators[key]
        return MockLocator(None, found=False)

    def get_by_label(self, label, **kw):
        return self.locator(label)

    async def goto(self, url, **kw):
        self.navigated_to.append(url)


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

FAKE_RULE_GRAPH = {
    "session_id": "exec_test_001",
    "mappings": [
        {
            "field_id": "rule_01",
            "source": {
                "primary_selector":   "name=Precio_Unitario",
                "fallback_selectors": ["id=txt-precio"],
                "label_hint":         "Precio del producto",
            },
            "destination": {
                "primary_selector":   "name=UnitCost",
                "fallback_selectors": ["id=input-unit-cost"],
                "label_hint":         "Unit Cost",
            },
            "transformation": {"type": "passthrough", "logic": "Use the value as-is."},
            "confidence": 0.95,
            "reasoning": "Precio del producto is Spanish for Unit Cost. Direct passthrough confirmed.",
        },
        {
            "field_id": "rule_02",
            "source": {
                "primary_selector":   "name=Cantidad",
                "fallback_selectors": ["id=txt-qty"],
                "label_hint":         "Cantidad",
            },
            "destination": {
                "primary_selector":   "name=quantity",
                "fallback_selectors": ["id=input-qty"],
                "label_hint":         "Quantity",
            },
            "transformation": {"type": "passthrough", "logic": "Use the value as-is."},
            "confidence": 0.98,
            "reasoning": "Cantidad is Spanish for Quantity. Direct passthrough.",
        },
    ],
}

FAKE_STEP = {
    "field_id":            "rule_01",
    "source_label":        "Precio del producto",
    "dest_label":          "Unit Cost",
    "source_name":         "name=Precio_Unitario",
    "dest_name":           "name=UnitCost",
    "value_read":          "150.50",
    "value_written":       "150.50",
    "transformation_type": "passthrough",
    "reasoning":           "Precio del producto is Spanish for Unit Cost.",
    "confidence":          0.95,
}


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

print("=" * 60)
print("1. _parse_selector() -- all selector variants")
print("=" * 60)
from app.execution.automation import _parse_selector

cases = [
    ("name=Precio_Unitario",      ("name",        "Precio_Unitario")),
    ("id=txt-precio",              ("id",          "txt-precio")),
    ("placeholder=Enter cost...", ("placeholder", "Enter cost...")),
    ("label=Unit Cost",            ("label",       "Unit Cost")),
    (".my-css-class",              ("raw",         ".my-css-class")),   # no = -> raw
    ("name=with=equals",           ("name",        "with=equals")),      # split on first = only
]
for raw, expected in cases:
    got = _parse_selector(raw)
    assert got == expected, f"_parse_selector({raw!r}) = {got!r}, expected {expected!r}"
    print(f"  {raw!r:35s} -> {got}")
print("  [PASS]\n")


print("=" * 60)
print("2. _apply_transformation() -- passthrough never calls the API")
print("=" * 60)
from app.execution.automation import _apply_transformation

async def test_passthrough():
    v1 = await _apply_transformation("150.50", {"type": "passthrough", "logic": "Use as-is."})
    assert v1 == "150.50", f"Got {v1!r}"
    print(f"  passthrough('150.50')         -> {v1!r}")

    v2 = await _apply_transformation("hello", {"type": "passthrough", "logic": ""})
    assert v2 == "hello"
    print(f"  passthrough('hello')          -> {v2!r}")

    # Empty logic string => fallback to passthrough even for non-passthrough types
    v3 = await _apply_transformation("42", {"type": "format", "logic": ""})
    assert v3 == "42"
    print(f"  format + empty logic('42')    -> {v3!r}  (falls back to passthrough)")

asyncio.run(test_passthrough())
print("  [PASS]\n")


print("=" * 60)
print("3. _build_narration_prompt() -- contains all step context fields")
print("=" * 60)
from app.execution.narrator import _build_narration_prompt

prompt = _build_narration_prompt(FAKE_STEP)
checks = {
    "source label":     "Precio del producto",
    "dest label":       "Unit Cost",
    "value_read":       "150.50",
    "transform type":   "passthrough",
    "confidence (95%)": "95%",
    "instruction":      "Narrate this",
}
for name, fragment in checks.items():
    assert fragment in prompt, f"Missing {name} ({fragment!r}) in prompt"
    print(f"  {name:20s}: found {fragment!r}")
print(f"  Prompt length: {len(prompt)} chars")
print("  [PASS]\n")


print("=" * 60)
print("4. AutomationEngine -- source field missing -> skip")
print("=" * 60)
from app.execution.automation import AutomationEngine

async def test_source_missing():
    broadcasts = []
    async def capture(m): broadcasts.append(m)

    page_a = MockPage(field_map={})                       # no fields on System A
    page_b = MockPage(field_map={"UnitCost": ""})
    engine = AutomationEngine(page_a, page_b, capture, "test_001")

    result = await engine._execute_rule(FAKE_RULE_GRAPH["mappings"][0])

    assert result["status"] == "skipped", f"Expected 'skipped', got {result['status']!r}"
    assert "source" in result["reason"],  f"Reason should mention 'source': {result['reason']!r}"

    skip_events = [e for e in broadcasts if e.get("status") == "skipped"]
    assert len(skip_events) == 1
    print(f"  status : {result['status']}")
    print(f"  reason : {result['reason']}")

asyncio.run(test_source_missing())
print("  [PASS]\n")


print("=" * 60)
print("5. AutomationEngine -- destination field missing -> skip")
print("=" * 60)

async def test_dest_missing():
    broadcasts = []
    async def capture(m): broadcasts.append(m)

    page_a = MockPage(field_map={"Precio_Unitario": "150.50"})  # source found
    page_b = MockPage(field_map={})                              # no fields on System B
    engine = AutomationEngine(page_a, page_b, capture, "test_001")

    result = await engine._execute_rule(FAKE_RULE_GRAPH["mappings"][0])

    assert result["status"] == "skipped",       f"Expected 'skipped', got {result['status']!r}"
    assert "destination" in result["reason"],   f"Reason: {result['reason']!r}"

    skip_events = [e for e in broadcasts if e.get("status") == "skipped"]
    assert len(skip_events) == 1
    print(f"  status : {result['status']}")
    print(f"  reason : {result['reason']}")

asyncio.run(test_dest_missing())
print("  [PASS]\n")


print("=" * 60)
print("6. AutomationEngine.execute() -- empty rule graph")
print("=" * 60)

async def test_empty_graph():
    broadcasts = []
    async def capture(m): broadcasts.append(m)

    engine = AutomationEngine(MockPage(), MockPage(), capture, "test_001")
    results = await engine.execute({"session_id": "x", "mappings": []})

    assert results == [], f"Expected [], got {results}"

    statuses = [e["status"] for e in broadcasts if e.get("type") == "phase_status"]
    assert "executing" in statuses, f"Missing 'executing' in {statuses}"
    assert "complete"  in statuses, f"Missing 'complete'  in {statuses}"

    complete = next(e["message"] for e in broadcasts
                    if e.get("type") == "phase_status" and e.get("status") == "complete")
    assert "0 field" in complete, f"Unexpected message: {complete!r}"
    print(f"  Phase broadcasts : {statuses}")
    print(f"  Completion msg   : {complete}")

asyncio.run(test_empty_graph())
print("  [PASS]\n")


print("=" * 60)
print("7. AutomationEngine.execute() -- full run with mock pages (narration mocked)")
print("=" * 60)

async def test_full_execute():
    broadcasts = []
    async def capture(m): broadcasts.append(m)

    page_a = MockPage(field_map={
        "Precio_Unitario": "150.50",
        "Cantidad":        "6",
    })
    page_b = MockPage(field_map={
        "UnitCost": "",
        "quantity": "",
    })
    engine = AutomationEngine(page_a, page_b, capture, "exec_test_001")

    with patch("app.execution.automation.narrate_step", new_callable=AsyncMock):
        results = await engine.execute(FAKE_RULE_GRAPH)

    # Shape
    assert len(results) == 2,              f"Expected 2 results, got {len(results)}"
    assert all(r["status"] == "done" for r in results), \
        f"Not all done: {[r['status'] for r in results]}"

    # Values read and written
    r1, r2 = results
    assert r1["value_read"]    == "150.50", f"r1 value_read:    {r1['value_read']!r}"
    assert r1["value_written"] == "150.50", f"r1 value_written: {r1['value_written']!r}"
    assert r2["value_read"]    == "6",      f"r2 value_read:    {r2['value_read']!r}"
    assert r2["value_written"] == "6",      f"r2 value_written: {r2['value_written']!r}"

    # Actual fills on page_b
    assert page_b._locators["UnitCost"].filled_with == "150.50", \
        f"UnitCost not filled: {page_b._locators.get('UnitCost')}"
    assert page_b._locators["quantity"].filled_with == "6", \
        f"quantity not filled: {page_b._locators.get('quantity')}"

    # Phase broadcasts
    phase_statuses = [e["status"] for e in broadcasts if e.get("type") == "phase_status"]
    assert "executing" in phase_statuses
    assert "complete"  in phase_statuses

    # Done step broadcasts (one per rule)
    done_events = [e for e in broadcasts if e.get("status") == "done"]
    assert len(done_events) == 2

    complete = next(e["message"] for e in broadcasts
                    if e.get("type") == "phase_status" and e.get("status") == "complete")
    print(f"  Completion: {complete}")
    for r in results:
        print(f"  [DONE] {r['source_label']} -> {r['dest_label']}"
              f"   read={r['value_read']!r}  written={r['value_written']!r}")

asyncio.run(test_full_execute())
print("  [PASS]\n")


print("=" * 60)
print("8. _apply_transformation('format') -- live Gemini Flash call")
print("=" * 60)

async def test_format_transform():
    result = await _apply_transformation(
        "150.5",
        {
            "type":  "format",
            "logic": "Format as a monetary value with exactly 2 decimal places and a $ prefix.",
        },
    )
    print(f"  Input  : '150.5'")
    print(f"  Output : {result!r}")
    assert result,       "Result must be non-empty"
    assert "150" in result, f"Expected '150' in transformed result, got {result!r}"

asyncio.run(test_format_transform())
print("  [PASS]\n")


print("=" * 60)
print("9. narrate_step() -- live Gemini call (TTS or text fallback)")
print("=" * 60)
from app.execution.narrator import narrate_step

async def test_narration():
    broadcasts = []
    async def capture(m): broadcasts.append(m)

    await narrate_step(FAKE_STEP, capture)

    assert len(broadcasts) >= 1,  "narrate_step must broadcast at least one event"
    ev = broadcasts[-1]
    assert ev["type"] == "narration",           f"Expected 'narration', got {ev['type']!r}"
    assert ev.get("text"),                      "Narration must have non-empty 'text'"
    assert ev["mode"] in ("audio", "text"),     f"Unknown mode: {ev['mode']!r}"

    print(f"  Mode : {ev['mode']}")
    print(f"  Text : {ev['text'][:120]}")
    if ev["mode"] == "audio":
        print(f"  Audio: {len(ev.get('audio_b64', ''))} base64 chars")

asyncio.run(test_narration())
print("  [PASS]\n")


print("=" * 60)
print("ALL EXECUTION TESTS PASSED")
print("=" * 60)
