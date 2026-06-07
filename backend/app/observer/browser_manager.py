import asyncio
import time
from pathlib import Path
from typing import Callable, Awaitable

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

_INJECT_JS = (Path(__file__).parent / "inject.js").read_text(encoding="utf-8")

# A value copied in System A must be pasted in System B within this window.
_COPY_WINDOW_SECS = 5.0
# How often the orphan-cleanup coroutine wakes up.
_CLEANUP_INTERVAL_SECS = 2.0


_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
    "--remote-debugging-port=9222",
]

async def _launch_browser(pw):
    """
    Try each channel in order so we use whatever browser is already installed.
    'chrome'  → Google Chrome  (most common on Windows)
    'msedge'  → Microsoft Edge (pre-installed on Windows 11)
    fallback  → Playwright's own Chromium (requires: playwright install chromium)
    """
    for channel in ("chrome", "msedge"):
        try:
            browser = await pw.chromium.launch(
                headless=False,
                channel=channel,
                args=_LAUNCH_ARGS,
            )
            return browser
        except Exception:
            pass  # channel not installed, try next

    # Last resort: Playwright's bundled Chromium
    return await pw.chromium.launch(
        headless=False,
        args=_LAUNCH_ARGS,
    )


class BrowserManager:
    """
    Opens a two-tab Playwright session (System A + System B), injects the
    DOM listener into every page, and correlates copy→paste pairs across tabs
    into confirmed mapping_pairs.  Raw events never leave this class.
    """

    def __init__(self, broadcast: Callable[[dict], Awaitable[None]] | None = None):
        # broadcast is an async callable supplied by main.py's WebSocket layer.
        # If None, events are captured silently (useful for unit tests).
        self._broadcast = broadcast or _noop_broadcast

        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page_a: Page | None = None
        self.page_b: Page | None = None

        self.session_id: str = ""
        self.url_a: str = ""
        self.url_b: str = ""

        # pending_copies: copied_value -> {source_context, timestamp}
        # Values expire after _COPY_WINDOW_SECS if never matched.
        self._pending_copies: dict[str, dict] = {}
        self._mapping_pairs: list[dict] = []
        self._pair_counter: int = 0

        self._cleanup_task: asyncio.Task | None = None

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    async def start_session(self, session_id: str, url_a: str, url_b: str) -> str:
        self.session_id = session_id
        self.url_a = url_a
        self.url_b = url_b
        self._pending_copies.clear()
        self._mapping_pairs.clear()
        self._pair_counter = 0

        self._playwright = await async_playwright().start()
        self._browser = await _launch_browser(self._playwright)
        self._context = await self._browser.new_context(
            # Clipboard read/write required for the async clipboard fallback in inject.js
            # (used when the source app — e.g. Google Sheets — sets clipboard
            # programmatically rather than via a text selection).
            permissions=["clipboard-read", "clipboard-write"],
        )
        # Grant clipboard permissions explicitly for Google domains so the
        # navigator.clipboard.readText() call inside inject.js doesn't throw.
        for origin in ["https://docs.google.com", "https://sheets.google.com"]:
            try:
                await self._context.grant_permissions(
                    ["clipboard-read", "clipboard-write"], origin=origin
                )
            except Exception:
                pass  # origin not loaded yet — permissions apply on first navigation

        self.page_a = await self._context.new_page()
        self.page_b = await self._context.new_page()

        # add_init_script re-injects on every navigation automatically.
        await self.page_a.add_init_script(_INJECT_JS)
        await self.page_b.add_init_script(_INJECT_JS)

        # expose_function also persists across navigations.
        # Must be registered before the first goto() so the function is available
        # when inject.js fires on document start.
        await self.page_a.expose_function("__arcana_push__", self._handler_a)
        await self.page_b.expose_function("__arcana_push__", self._handler_b)

        await self.page_a.goto(url_a, wait_until="domcontentloaded")
        await self.page_b.goto(url_b, wait_until="domcontentloaded")

        self._cleanup_task = asyncio.create_task(self._orphan_cleanup_loop())

        await self._broadcast({
            "type": "phase_status",
            "phase": 1,
            "status": "observing",
            "message": "ARCANA is watching. Perform the mapping manually now.",
        })

        return session_id

    async def stop_session(self) -> dict:
        """Stop observation and return the completed Session Trace."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

        return self.get_session_trace()

    def get_session_trace(self) -> dict:
        return {
            "session_id": self.session_id,
            "system_a_url": self.url_a,
            "system_b_url": self.url_b,
            "mapping_pairs": list(self._mapping_pairs),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Internal event handlers
    # (Playwright calls these synchronously from JS; we schedule async work.)
    # ─────────────────────────────────────────────────────────────────────────

    def _handler_a(self, event_data: dict) -> None:
        asyncio.ensure_future(self._handle_event(event_data, "system_a"))

    def _handler_b(self, event_data: dict) -> None:
        asyncio.ensure_future(self._handle_event(event_data, "system_b"))

    async def _handle_event(self, event_data: dict, system: str) -> None:
        event_type: str = event_data.get("event", "")
        value: str = (event_data.get("value") or "").strip()
        context: dict = dict(event_data.get("context") or {})
        context["system"] = system

        # Confirm inject.js is alive — show in feed but don't treat as a mapping event
        if event_type == "inject_ready":
            await self._broadcast({
                "type": "observation_event",
                "event": "inject_ready",
                "system": system,
                "message": f"inject.js ready on {system}",
            })
            return

        if not value:
            return

        # Broadcast observation to frontend so the live feed stays active
        field = context.get("label") or context.get("name") or context.get("id") or "?"
        short_val = value[:40] + ("…" if len(value) > 40 else "")
        await self._broadcast({
            "type": "observation_event",
            "event": event_type,
            "value": value,
            "system": system,
            "field": field,
            "message": f"[{system}] {event_type}: '{short_val}' in '{field}'",
        })

        # ── Cross-tab correlation ──────────────────────────────────────────

        if system == "system_a" and event_type == "copy":
            # Store for potential match from System B
            self._pending_copies[value] = {
                "source_context": context,
                "timestamp": time.monotonic(),
            }
            return

        if system == "system_b" and event_type in ("paste", "input"):
            pending = self._pending_copies.pop(value, None)
            if pending is None:
                # Typed value with no matching copy — not a mapped pair
                return

            self._pair_counter += 1
            pair = {
                "pair_id": self._pair_counter,
                "action": event_type,
                "copied_value": value,
                "source_context": pending["source_context"],
                "destination_context": context,
            }
            self._mapping_pairs.append(pair)

            await self._broadcast({
                "type": "mapping_pair",
                "pair_id": self._pair_counter,
                "value": value,
                "source_label": pending["source_context"].get("label") or pending["source_context"].get("name") or "?",
                "dest_label": context.get("label") or context.get("name") or "?",
                "message": f"Pair {self._pair_counter} confirmed: "
                           f'"{pending["source_context"].get("label") or "?"}" → '
                           f'"{context.get("label") or "?"}"',
            })

    # ─────────────────────────────────────────────────────────────────────────
    # Orphan cleanup
    # ─────────────────────────────────────────────────────────────────────────

    async def _orphan_cleanup_loop(self) -> None:
        """
        Evict pending_copies entries older than _COPY_WINDOW_SECS.
        These are values the user copied but never pasted — noise that must
        never reach Gemini.
        """
        while True:
            await asyncio.sleep(_CLEANUP_INTERVAL_SECS)
            cutoff = time.monotonic() - _COPY_WINDOW_SECS
            expired = [v for v, d in self._pending_copies.items() if d["timestamp"] < cutoff]
            for v in expired:
                del self._pending_copies[v]
            if expired:
                await self._broadcast({
                    "type": "debug",
                    "message": f"Orphan cleanup: evicted {len(expired)} unmatched copy event(s).",
                })


async def _noop_broadcast(_: dict) -> None:
    pass
