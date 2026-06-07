"""
main.py — FastAPI application, all routes, WebSocket hub.

Phase orchestration:
  POST /observe             → Phase I  : launch Playwright observation session
  POST /learn/{session_id}  → Phase II : stop observation, call Gemini Pro, build Rule Graph
  POST /execute/{session_id}→ Phase III: run AutomationEngine against new data
  WS   /ws/{session_id}     → Real-time event stream for all three phases
"""

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright
from pydantic import BaseModel

from app.arcanum.brain import run_learning_phase
from app.execution.automation import AutomationEngine
from app.observer.browser_manager import BrowserManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# Warn loudly if uvicorn's reloader spawned this process — Playwright GUI
# windows won't open from inside a reload subprocess on Windows.
if os.environ.get("WATCHFILES_FORCE_POLLING") or os.environ.get("WEB_CONCURRENCY"):
    logger.warning(
        "⚠  Reload/worker mode detected. Playwright browser windows may not open. "
        "Start with: uvicorn app.main:app --port 8000  (no --reload flag)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    session_id: str
    url_a: str = ""
    url_b: str = ""
    websocket: WebSocket | None = None
    # Serialises concurrent WebSocket sends (browser events + narration fire in parallel)
    ws_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Buffer events that arrive before the WebSocket connects, flush on connect
    message_buffer: list = field(default_factory=list)
    browser_manager: BrowserManager | None = None
    rule_graph: dict | None = None  # the inner "mappings" dict, not the wrapper


# In-memory store — one demo process, one session at a time is fine for the hackathon
_sessions: dict[str, SessionState] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Broadcast factory
# ─────────────────────────────────────────────────────────────────────────────

def _make_broadcast(session_id: str):
    """
    Returns an async callable that sends a JSON dict to the session's WebSocket.
    If the WebSocket is not yet connected, the message is buffered and replayed
    on connection — prevents losing the first phase_status events.
    """
    async def broadcast(msg: dict) -> None:
        state = _sessions.get(session_id)
        if not state:
            return
        if state.websocket is None:
            state.message_buffer.append(msg)
            return
        async with state.ws_lock:
            try:
                await state.websocket.send_json(msg)
            except Exception as exc:
                logger.warning("WebSocket send failed for %s: %s", session_id, exc)

    return broadcast


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="ARCANA", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve mock destination forms at http://localhost:8000/mock/system_b_arca_form.html etc.
_MOCK_DIR = Path(__file__).parent.parent.parent / "mock_systems"
app.mount("/mock", StaticFiles(directory=str(_MOCK_DIR)), name="mock")


# ─────────────────────────────────────────────────────────────────────────────
# Request / response models
# ─────────────────────────────────────────────────────────────────────────────

class ObserveRequest(BaseModel):
    url_a: str
    url_b: str

class ExecuteRequest(BaseModel):
    url_a: str | None = None  # if None, re-use URL from observation session
    url_b: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — /ws/{session_id}
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    logger.info("WebSocket connected: %s", session_id)

    # Create session record if the frontend connects before /observe is called
    if session_id not in _sessions:
        _sessions[session_id] = SessionState(session_id=session_id)

    state = _sessions[session_id]
    state.websocket = websocket

    # Flush any events that were buffered before the WS connected
    if state.message_buffer:
        async with state.ws_lock:
            for msg in state.message_buffer:
                try:
                    await websocket.send_json(msg)
                except Exception:
                    break
            state.message_buffer.clear()

    # Confirm connection to the frontend
    await websocket.send_json({
        "type": "connected",
        "session_id": session_id,
        "message": "ARCANA is online and listening.",
    })

    try:
        # Keep the connection alive; frontend may send pings or control messages
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: %s", session_id)
        state.websocket = None


# ─────────────────────────────────────────────────────────────────────────────
# Phase I — POST /observe
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/observe")
async def observe(body: ObserveRequest):
    """
    Start Phase I. Opens a two-tab Playwright browser and begins listening
    for copy/paste events. Returns immediately so the frontend can connect
    to the WebSocket before events start flowing.
    """
    session_id = f"arcana_{uuid.uuid4().hex[:8]}"
    _sessions[session_id] = SessionState(
        session_id=session_id,
        url_a=body.url_a,
        url_b=body.url_b,
    )
    logger.info("New session: %s | A=%s | B=%s", session_id, body.url_a, body.url_b)

    # Launch in background — returns session_id before browser opens
    asyncio.create_task(_run_observation(session_id, body.url_a, body.url_b))

    return {"session_id": session_id, "status": "starting"}


async def _run_observation(session_id: str, url_a: str, url_b: str) -> None:
    state = _sessions[session_id]
    broadcast = _make_broadcast(session_id)
    bm = BrowserManager(broadcast=broadcast)
    state.browser_manager = bm
    try:
        await bm.start_session(session_id, url_a, url_b)
        # Browser stays open; human performs the mapping manually.
        # Phase I ends when POST /learn is called.
    except Exception as exc:
        logger.error("Observation failed for %s: %s", session_id, exc)
        await broadcast({
            "type": "phase_status",
            "phase": 1,
            "status": "error",
            "message": f"Observation error: {exc}",
        })


# ─────────────────────────────────────────────────────────────────────────────
# Phase II — POST /learn/{session_id}
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/learn/{session_id}")
async def learn(session_id: str):
    """
    End Phase I and start Phase II.
    Stops observation, retrieves the Session Trace, calls Gemini Pro,
    stores the Rule Graph, and returns it.
    """
    state = _sessions.get(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="Session not found.")
    if not state.browser_manager:
        raise HTTPException(status_code=400, detail="No active observation session for this ID.")

    broadcast = _make_broadcast(session_id)

    # Close the observation browser and collect the Session Trace
    session_trace = await state.browser_manager.stop_session()
    state.browser_manager = None

    pairs = session_trace.get("mapping_pairs", [])
    logger.info("Session %s — observation ended, %d pair(s) captured", session_id, len(pairs))

    if not pairs:
        raise HTTPException(
            status_code=400,
            detail="No mapping pairs were captured. Make sure you copied values from System A and pasted them into System B.",
        )

    # Phase II — Gemini Pro reasoning
    result = await run_learning_phase(session_trace, broadcast)

    # Store the inner graph (the dict with "mappings" key)
    state.rule_graph = result["rule_graph"]

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Phase III — POST /execute/{session_id}
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/execute/{session_id}")
async def execute(session_id: str, body: ExecuteRequest):
    """
    Start Phase III. Opens a fresh browser, runs AutomationEngine against
    the stored Rule Graph, and streams every step over WebSocket.
    """
    state = _sessions.get(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="Session not found.")
    if not state.rule_graph:
        raise HTTPException(status_code=400, detail="No Rule Graph found. Run /learn first.")

    broadcast = _make_broadcast(session_id)

    url_a = body.url_a or state.url_a
    url_b = body.url_b or state.url_b

    # Open a fresh Playwright browser for Phase III (reuses same channel logic)
    from app.observer.browser_manager import _launch_browser
    pw      = await async_playwright().start()
    browser = await _launch_browser(pw)
    context = await browser.new_context()
    page_a  = await context.new_page()
    page_b  = await context.new_page()

    await page_a.goto(url_a, wait_until="load")
    await page_b.goto(url_b, wait_until="load")

    engine = AutomationEngine(
        page_a=page_a,
        page_b=page_b,
        broadcast=broadcast,
        session_id=session_id,
    )

    try:
        results = await engine.execute(state.rule_graph)
    finally:
        await context.close()
        await browser.close()
        await pw.stop()

    return {"session_id": session_id, "results": results}


# ─────────────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "active_sessions": len(_sessions)}


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Frontend — MUST be mounted LAST so API routes are checked first.
# Mount("/") at any earlier position would intercept all POST/WS requests.
# ─────────────────────────────────────────────────────────────────────────────

_FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
