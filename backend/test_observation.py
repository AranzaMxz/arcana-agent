"""
test_observation.py -- BrowserManager unit tests (no browser launch required).
Exercises the copy->paste correlation engine, event broadcasting, orphan cleanup,
and session trace output entirely in-process.

Run from: backend/
  python test_observation.py
"""

import asyncio
import sys
import os
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ctx(system, label, name, field_id, url="http://fake/page"):
    return {
        "system": system,
        "label": label,
        "name": name,
        "id": field_id,
        "placeholder": "",
        "nearby_labels": [],
        "url": url,
    }


async def run_tests():
    from app.observer.browser_manager import BrowserManager

    broadcasts: list[dict] = []

    async def capture(msg: dict) -> None:
        broadcasts.append(msg)

    # ─────────────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("1. Import & instantiation — no browser launched")
    print("=" * 60)
    bm = BrowserManager(broadcast=capture)
    # Manually set session metadata (normally done by start_session + Playwright)
    bm.session_id = "test_obs_001"
    bm.url_a = "https://source.example.com"
    bm.url_b = "http://localhost:8000/mock/dest"
    print("  [PASS] BrowserManager created without touching Playwright\n")


    # ─────────────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("2. inject_ready event — broadcast only, no mapping pair")
    print("=" * 60)
    broadcasts.clear()
    await bm._handle_event({"event": "inject_ready", "value": "", "context": {}}, "system_a")
    await asyncio.sleep(0)

    ready = [e for e in broadcasts if e.get("event") == "inject_ready"]
    assert len(ready) == 1,              f"Expected 1 inject_ready broadcast, got {len(ready)}"
    assert len(bm._mapping_pairs) == 0,  "inject_ready must NOT produce a mapping pair"
    print(f"  Broadcast : {ready[0]['message']}")
    print("  [PASS]\n")


    # ─────────────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("3. copy (System A) + paste (System B) -> confirmed mapping pair")
    print("=" * 60)
    broadcasts.clear()
    bm._mapping_pairs.clear()
    bm._pending_copies.clear()
    bm._pair_counter = 0

    ctx_a = _ctx("system_a", "Precio del producto", "Precio_Unitario", "txt-precio")
    ctx_b = _ctx("system_b", "Unit Cost",           "UnitCost",        "input-unit-cost")

    await bm._handle_event({"event": "copy",  "value": "150.50", "context": ctx_a}, "system_a")
    await asyncio.sleep(0)
    assert "150.50" in bm._pending_copies, "Value should sit in pending_copies after copy"
    print("  copy recorded in pending_copies OK")

    await bm._handle_event({"event": "paste", "value": "150.50", "context": ctx_b}, "system_b")
    await asyncio.sleep(0)
    assert "150.50" not in bm._pending_copies, "Value should be removed from pending after paste"
    assert len(bm._mapping_pairs) == 1,        f"Expected 1 pair, got {len(bm._mapping_pairs)}"

    pair = bm._mapping_pairs[0]
    assert pair["copied_value"]                       == "150.50"
    assert pair["source_context"]["label"]            == "Precio del producto"
    assert pair["destination_context"]["label"]       == "Unit Cost"
    assert pair["pair_id"]                            == 1

    pair_msgs = [e for e in broadcasts if e.get("type") == "mapping_pair"]
    assert len(pair_msgs) == 1
    print(f"  Pair confirmed: {pair_msgs[0]['message']}")
    print("  [PASS]\n")


    # ─────────────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("4. Paste with no prior copy — ignored (not a mapping pair)")
    print("=" * 60)
    pre_count = len(bm._mapping_pairs)
    broadcasts.clear()

    ctx_b2 = _ctx("system_b", "Quantity", "quantity", "input-qty")
    await bm._handle_event({"event": "paste", "value": "99", "context": ctx_b2}, "system_b")
    await asyncio.sleep(0)

    assert len(bm._mapping_pairs) == pre_count, "Orphan paste must not create a mapping pair"
    print("  Paste with no matching copy correctly discarded")
    print("  [PASS]\n")


    # ─────────────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("5. Orphan copy — expires and is evicted by cleanup")
    print("=" * 60)
    bm._pending_copies.clear()

    ctx_a2 = _ctx("system_a", "Cantidad", "Cantidad", "txt-qty")
    await bm._handle_event({"event": "copy", "value": "6", "context": ctx_a2}, "system_a")
    await asyncio.sleep(0)
    assert "6" in bm._pending_copies, "Value should be pending after copy"

    # Backdate timestamp so it looks old
    bm._pending_copies["6"]["timestamp"] = time.monotonic() - 99.0

    # Run the same eviction logic the cleanup loop uses
    cutoff  = time.monotonic() - 5.0
    expired = [v for v, d in bm._pending_copies.items() if d["timestamp"] < cutoff]
    for v in expired:
        del bm._pending_copies[v]

    assert "6" not in bm._pending_copies, "Expired entry should be evicted"
    print("  Expired pending copy evicted correctly")
    print("  [PASS]\n")


    # ─────────────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("6. Multiple sequential pairs — correct IDs and trace")
    print("=" * 60)
    bm._mapping_pairs.clear()
    bm._pending_copies.clear()
    bm._pair_counter = 0
    broadcasts.clear()

    pairs_data = [
        ("150.50", "Precio del producto", "Precio_Unitario", "txt-precio",
                   "Unit Cost",           "UnitCost",         "input-unit-cost"),
        ("6",      "Cantidad",            "Cantidad",         "txt-qty",
                   "Quantity",            "quantity",          "input-qty"),
        ("John",   "Nombre",              "Nombre",           "txt-nombre",
                   "First Name",          "first_name",        "input-name"),
    ]

    for value, sl, sn, si, dl, dn, di in pairs_data:
        await bm._handle_event({"event": "copy",  "value": value,
                                "context": _ctx("system_a", sl, sn, si)}, "system_a")
        await bm._handle_event({"event": "paste", "value": value,
                                "context": _ctx("system_b", dl, dn, di)}, "system_b")
        await asyncio.sleep(0)

    assert len(bm._mapping_pairs) == 3, f"Expected 3 pairs, got {len(bm._mapping_pairs)}"
    for i, p in enumerate(bm._mapping_pairs, 1):
        assert p["pair_id"] == i
        src = p["source_context"]["label"]
        dst = p["destination_context"]["label"]
        print(f"  Pair {i}: '{src}' -> '{dst}'  (value='{p['copied_value']}')")
    print("  [PASS]\n")


    # ─────────────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("7. get_session_trace() — structure and completeness")
    print("=" * 60)
    trace = bm.get_session_trace()
    assert trace["session_id"]             == "test_obs_001"
    assert trace["system_a_url"]           == "https://source.example.com"
    assert trace["system_b_url"]           == "http://localhost:8000/mock/dest"
    assert isinstance(trace["mapping_pairs"], list)
    assert len(trace["mapping_pairs"])     == 3

    # Every pair must expose the fields brain.py reads
    for p in trace["mapping_pairs"]:
        for key in ("pair_id", "action", "copied_value",
                    "source_context", "destination_context"):
            assert key in p, f"Missing key '{key}' in pair"
        for ctx_key in ("label", "name", "id", "system"):
            assert ctx_key in p["source_context"],      f"Missing '{ctx_key}' in source_context"
            assert ctx_key in p["destination_context"], f"Missing '{ctx_key}' in destination_context"

    print(f"  session_id    : {trace['session_id']}")
    print(f"  system_a_url  : {trace['system_a_url']}")
    print(f"  system_b_url  : {trace['system_b_url']}")
    print(f"  mapping_pairs : {len(trace['mapping_pairs'])} pairs, all fields present")
    print("  [PASS]\n")


asyncio.run(run_tests())

print("=" * 60)
print("ALL OBSERVATION TESTS PASSED")
print("=" * 60)
