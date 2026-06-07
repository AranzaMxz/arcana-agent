"use strict";

// ─────────────────────────────────────────────────────────────────────────────
// Configuration
// ─────────────────────────────────────────────────────────────────────────────

const API_BASE = "http://localhost:8000";
const WS_BASE  = "ws://localhost:8000/ws/";

// Pair preset URLs — change these at any time; nothing else in the code is pair-specific.
const PAIRS = {
    a: {
        urlA: "https://docs.google.com/spreadsheets/d/1JdktARAx4DTcIy0FqIGyZS-knHTjxbop46Oy-jvVA_w/edit?gid=943090944#gid=943090944",
        urlB: "https://docs.google.com/spreadsheets/d/1-2l0D7jOp6IrrNAEBiqXrY9b6XmgxrcohV6UW2Allmo/edit?gid=1483315145#gid=1483315145",
    },
    b: {
        urlA: "https://automationexercise.com",
        urlB: "http://localhost:8000/mock/system_b_inventory_form.html",
    },
};

// ─────────────────────────────────────────────────────────────────────────────
// DOM references
// ─────────────────────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

const urlAInput   = $("url-a");
const urlBInput   = $("url-b");
const pairABtn    = $("pair-a-btn");
const pairBBtn    = $("pair-b-btn");
const startBtn    = $("start-btn");
const learnBtn    = $("learn-btn");
const execBtn     = $("exec-btn");
const phaseBar    = $("phase-bar");
const phaseText   = $("phase-text");
const eventFeed   = $("event-feed");
const graphPanel  = $("rule-graph-panel");
const emptyState  = $("empty-state");
const execPanel   = $("exec-panel");
const execLog     = $("exec-log");
const narBar      = $("narration-bar");
const narText     = $("narration-text");

// ─────────────────────────────────────────────────────────────────────────────
// Session state
// ─────────────────────────────────────────────────────────────────────────────

let sessionId  = null;
let socket     = null;
let audioCtx   = null;
let narTimer   = null;
let phaseTimer = null;   // for animated dots during Phase II

// ─────────────────────────────────────────────────────────────────────────────
// Buffered rendering (150 ms flush — per spec)
// ─────────────────────────────────────────────────────────────────────────────

const eventBuf = [];
const execBuf  = [];
const FLUSH_MS = 150;

setInterval(() => {
    if (!eventBuf.length) return;
    const frag = document.createDocumentFragment();
    while (eventBuf.length) {
        const el = buildEventEl(eventBuf.shift());
        if (el) frag.appendChild(el);
    }
    eventFeed.appendChild(frag);
    eventFeed.scrollTop = eventFeed.scrollHeight;
}, FLUSH_MS);

setInterval(() => {
    if (!execBuf.length) return;
    const frag = document.createDocumentFragment();
    while (execBuf.length) {
        const el = buildExecStepEl(execBuf.shift());
        if (el) frag.appendChild(el);
    }
    execLog.appendChild(frag);
    execLog.scrollTop = execLog.scrollHeight;
}, FLUSH_MS);

// ─────────────────────────────────────────────────────────────────────────────
// WebSocket
// ─────────────────────────────────────────────────────────────────────────────

function connectWS(sid) {
    if (socket) {
        try { socket.close(); } catch (_) { /* ignore */ }
    }
    socket = new WebSocket(WS_BASE + sid);

    socket.onopen = () => {
        addDirectFeedEntry("WebSocket connected.", "sys-ev");
    };

    socket.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        dispatch(msg);
    };

    socket.onclose = () => {
        addDirectFeedEntry("WebSocket closed.", "sys-ev");
    };

    socket.onerror = () => {
        addDirectFeedEntry("WebSocket error — check backend is running.", "err-ev");
    };

    // Keep-alive ping every 20 s
    setInterval(() => {
        if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send("ping");
        }
    }, 20_000);
}

// ─────────────────────────────────────────────────────────────────────────────
// Message dispatcher
// ─────────────────────────────────────────────────────────────────────────────

function dispatch(msg) {
    switch (msg.type) {
        case "pong":
            break;

        case "connected":
            addDirectFeedEntry(msg.message || "ARCANA connected.", "sys-ev");
            break;

        case "phase_status":
            applyPhaseStatus(msg);
            eventBuf.push(msg);
            if (msg.phase === 2 && msg.status === "complete" && msg.rule_graph) {
                renderRuleGraph(msg.rule_graph);
            }
            if (msg.phase === 3 && msg.status === "complete") {
                execBtn.disabled = false;
            }
            break;

        case "observation_event":
        case "mapping_pair":
            eventBuf.push(msg);
            break;

        case "execution_step":
            eventBuf.push(msg);
            if (msg.status === "done" || msg.status === "skipped") {
                execBuf.push(msg);
            }
            break;

        case "narration":
            handleNarration(msg);
            break;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Phase status bar
// ─────────────────────────────────────────────────────────────────────────────

function applyPhaseStatus(msg) {
    // Clear animated dots from previous Phase II
    if (phaseTimer) {
        clearInterval(phaseTimer);
        phaseTimer = null;
    }

    phaseBar.className = "phase-" + msg.phase;

    // Animated dots during Gemini reasoning
    if (msg.phase === 2 && msg.status === "reasoning") {
        const baseMsg = (msg.message || "ARCANA is reasoning").replace(/\.+$/, "");
        let dots = 1;
        phaseText.textContent = baseMsg + ".";
        phaseTimer = setInterval(() => {
            dots = (dots % 3) + 1;
            phaseText.textContent = baseMsg + ".".repeat(dots);
        }, 400);
    } else {
        phaseText.textContent = msg.message || "";
    }

    // Unlock buttons on milestone events
    if (msg.phase === 2 && msg.status === "complete") {
        execBtn.disabled = false;
    }
}

function setPhaseDisplay(phase, text) {
    phaseBar.className = "phase-" + phase;
    phaseText.textContent = text;
}

// ─────────────────────────────────────────────────────────────────────────────
// Rule Graph renderer
// ─────────────────────────────────────────────────────────────────────────────

function renderRuleGraph(graph) {
    const mappings = Array.isArray(graph.mappings) ? graph.mappings : [];

    // Clear panel (keeps the DOM small — no accumulation on re-learn)
    graphPanel.innerHTML = "";

    // Header
    const hdr = document.createElement("div");
    hdr.className = "rg-header";
    hdr.innerHTML = `
        Rule Graph
        <span class="rg-count">${mappings.length} rule${mappings.length !== 1 ? "s" : ""} learned</span>
    `;
    graphPanel.appendChild(hdr);

    for (const rule of mappings) {
        graphPanel.appendChild(buildRuleCard(rule));
    }

    execBtn.disabled = false;
}

function buildRuleCard(rule) {
    const src  = rule.source        || {};
    const dst  = rule.destination   || {};
    const tf   = rule.transformation || {};
    const conf = parseFloat(rule.confidence ?? 1);

    const srcLabel = src.label_hint || src.primary_selector || "?";
    const dstLabel = dst.label_hint || dst.primary_selector || "?";
    const tType    = tf.type || "passthrough";

    const confClass = conf >= 0.85 ? "c-high" : conf >= 0.70 ? "c-mid" : "c-low";
    const stepClass = conf >= 0.85 ? "done"   : conf >= 0.70 ? "mid"  : "low";

    const card = document.createElement("div");
    card.className = "rule-card";

    card.innerHTML = `
        <div class="rule-card-header">
            <div class="field-mapping">
                <span class="src-label">${esc(srcLabel)}</span>
                <span class="map-arrow">→</span>
                <span class="dst-label">${esc(dstLabel)}</span>
            </div>
            <div class="rule-badges">
                <span class="t-pill t-${esc(tType)}">${esc(tType)}</span>
                <span class="conf-badge ${confClass}">${(conf * 100).toFixed(0)}%</span>
            </div>
        </div>
        <div class="rule-reasoning">${esc(rule.reasoning || "")}</div>
        ${tf.logic ? `<div class="rule-logic">${esc(tf.logic)}</div>` : ""}
    `;
    return card;
}

// ─────────────────────────────────────────────────────────────────────────────
// Event feed entry builders
// ─────────────────────────────────────────────────────────────────────────────

function buildEventEl(msg) {
    const div = document.createElement("div");
    const ts  = nowStr();

    if (msg.type === "phase_status") {
        div.className = "ev phase-ev";
        div.innerHTML = `<span class="ev-time">${ts}</span><span class="ev-tag tag-sys">PH${msg.phase}</span>${esc(msg.message || "")}`;

    } else if (msg.type === "mapping_pair") {
        div.className = "ev pair-ev";
        const sl = msg.source_label || "?";
        const dl = msg.dest_label   || "?";
        div.innerHTML = `<span class="ev-time">${ts}</span><span class="ev-tag tag-pair">PAIR</span>${esc(sl)} → ${esc(dl)}`;

    } else if (msg.type === "observation_event") {
        div.className = "ev obs-ev";
        const text = msg.message || msg.action || JSON.stringify(msg);
        div.innerHTML = `<span class="ev-time">${ts}</span><span class="ev-tag tag-obs">OBS</span>${esc(String(text).slice(0, 120))}`;

    } else if (msg.type === "execution_step") {
        div.className = "ev exec-ev";
        div.innerHTML = `<span class="ev-time">${ts}</span><span class="ev-tag tag-exec">EXEC</span>${esc(msg.message || "")}`;

    } else {
        return null;
    }
    return div;
}

function addDirectFeedEntry(text, cls = "") {
    const div = document.createElement("div");
    div.className = "ev" + (cls ? " " + cls : "");
    div.innerHTML = `<span class="ev-time">${nowStr()}</span>${esc(text)}`;
    eventFeed.appendChild(div);
    eventFeed.scrollTop = eventFeed.scrollHeight;
}

// ─────────────────────────────────────────────────────────────────────────────
// Execution log entry builder
// ─────────────────────────────────────────────────────────────────────────────

function buildExecStepEl(msg) {
    const div  = document.createElement("div");
    const conf = parseFloat(msg.confidence ?? 0);

    if (msg.status === "done") {
        const cc = conf >= 0.85 ? "done" : conf >= 0.70 ? "mid" : "low";
        const cb = conf >= 0.85 ? "c-high" : conf >= 0.70 ? "c-mid" : "c-low";
        div.className = "exec-step " + cc;
        div.innerHTML = `
            <div class="exec-step-hdr">
                <span>${esc(msg.source_label || "?")} → ${esc(msg.dest_label || "?")}</span>
                <span class="conf-badge ${cb}">${(conf * 100).toFixed(0)}%</span>
            </div>
            <div class="exec-step-vals">
                <span class="val-read">${esc(String(msg.value_read ?? ""))}</span>
                <span class="val-arrow"> → </span>
                <span class="val-written">${esc(String(msg.value_written ?? ""))}</span>
            </div>
        `;
    } else if (msg.status === "skipped") {
        div.className = "exec-step skip";
        div.textContent = `⚠ Skipped: ${msg.source_label || "?"} → ${msg.dest_label || "?"} — ${msg.reason || "field not found"}`;
    }
    return div;
}

// ─────────────────────────────────────────────────────────────────────────────
// Narration (audio + text)
// ─────────────────────────────────────────────────────────────────────────────

function handleNarration(msg) {
    if (msg.text) {
        narText.textContent = `"${msg.text}"`;
        narBar.classList.add("visible");
        clearTimeout(narTimer);
        narTimer = setTimeout(() => narBar.classList.remove("visible"), 8_000);
    }

    if (msg.mode === "audio" && msg.audio_b64) {
        playPCM(
            msg.audio_b64,
            msg.sample_rate || 24_000,
            msg.channels    || 1,
            msg.bit_depth   || 16,
        ).catch((e) => console.warn("Audio playback:", e));
    }
}

async function playPCM(b64, rate, channels, bits) {
    // Create AudioContext on first use (requires a user gesture, but buttons
    // already satisfy that requirement before execution ever fires audio).
    if (!audioCtx) {
        audioCtx = new AudioContext({ sampleRate: rate });
    }
    if (audioCtx.state === "suspended") {
        await audioCtx.resume();
    }

    const raw   = atob(b64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);

    const bytesPerSample = bits / 8;
    const samples = bytes.length / bytesPerSample;
    const buf   = audioCtx.createBuffer(channels, samples, rate);
    const view  = new DataView(bytes.buffer);
    const f32   = new Float32Array(samples);

    for (let i = 0; i < samples; i++) {
        // 16-bit little-endian PCM → float32 [-1, 1]
        f32[i] = view.getInt16(i * 2, true) / 32_768.0;
    }
    buf.copyToChannel(f32, 0);

    const src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(audioCtx.destination);
    src.start();
}

// ─────────────────────────────────────────────────────────────────────────────
// API calls
// ─────────────────────────────────────────────────────────────────────────────

async function startObservation() {
    const a = urlAInput.value.trim();
    const b = urlBInput.value.trim();
    if (!a || !b) {
        alert("Both system URLs are required before starting observation.");
        return;
    }

    // Reset UI
    startBtn.disabled = true;
    learnBtn.disabled = false;
    execBtn.disabled  = true;
    eventFeed.innerHTML = "";
    execLog.innerHTML   = "";
    graphPanel.innerHTML = '<div class="empty-state"><div class="empty-glyph">◈</div><p>Observation in progress…</p></div>';
    narBar.classList.remove("visible");

    setPhaseDisplay(1, "Phase I — Starting observation…");

    let data;
    try {
        const res = await fetch(`${API_BASE}/observe`, {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({ url_a: a, url_b: b }),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || res.statusText);
        }
        data = await res.json();
    } catch (e) {
        alert("Failed to start observation: " + e.message);
        startBtn.disabled = false;
        return;
    }

    sessionId = data.session_id;
    connectWS(sessionId);
    setPhaseDisplay(1, `Phase I — Observing [${sessionId}]`);
}

async function stopAndLearn() {
    if (!sessionId) return;

    learnBtn.disabled = true;
    setPhaseDisplay(2, "Phase II — Stopping observation…");

    let data;
    try {
        const res = await fetch(`${API_BASE}/learn/${sessionId}`, {
            method: "POST",
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || res.statusText);
        }
        data = await res.json();
    } catch (e) {
        alert("Learning failed: " + e.message);
        learnBtn.disabled = false;
        return;
    }

    // Render rule graph from HTTP response as a fallback in case the WS event
    // arrived before the graph panel was ready.
    if (data.rule_graph && !graphPanel.querySelector(".rule-card")) {
        renderRuleGraph(data.rule_graph);
    }

    execBtn.disabled = false;
}

async function runExecution() {
    if (!sessionId) return;

    execBtn.disabled  = true;
    execLog.innerHTML = "";
    setPhaseDisplay(3, "Phase III — Executing autonomously…");

    try {
        const res = await fetch(`${API_BASE}/execute/${sessionId}`, {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({}),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || res.statusText);
        }
    } catch (e) {
        alert("Execution failed: " + e.message);
        execBtn.disabled = false;
        return;
    }

    execBtn.disabled = false;
}

// ─────────────────────────────────────────────────────────────────────────────
// Pair presets
// ─────────────────────────────────────────────────────────────────────────────

pairABtn.addEventListener("click", () => {
    urlAInput.value = PAIRS.a.urlA;
    urlBInput.value = PAIRS.a.urlB;
});

pairBBtn.addEventListener("click", () => {
    urlAInput.value = PAIRS.b.urlA;
    urlBInput.value = PAIRS.b.urlB;
});

// ─────────────────────────────────────────────────────────────────────────────
// Button event listeners
// ─────────────────────────────────────────────────────────────────────────────

startBtn.addEventListener("click", startObservation);
learnBtn.addEventListener("click", stopAndLearn);
execBtn.addEventListener("click",  runExecution);

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

function esc(s) {
    return String(s)
        .replace(/&/g,  "&amp;")
        .replace(/</g,  "&lt;")
        .replace(/>/g,  "&gt;")
        .replace(/"/g,  "&quot;")
        .replace(/'/g,  "&#039;");
}

function nowStr() {
    const d = new Date();
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
        .map((n) => String(n).padStart(2, "0"))
        .join(":");
}
