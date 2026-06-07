(function () {
  // Idempotency guard — safe to inject on every navigation
  if (window.__arcana_injected__) return;
  window.__arcana_injected__ = true;

  // ─── Layer 1: Tag whitelist ───────────────────────────────────────────────
  const FORM_FIELD_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);

  function isFormField(el) {
    return el && FORM_FIELD_TAGS.has(el.tagName);
  }

  // copy events use a relaxed check because source systems often display
  // values in <span>/<td> elements, not just form inputs.
  // DEMO RISK: if source system renders prices only in non-focusable elements,
  // activeElement will be <body>; context will be sparse but copy value is captured.
  function isMeaningfulElement(el) {
    if (!el || !el.tagName) return false;
    const NOISE_TAGS = new Set(['HTML', 'BODY', 'MAIN', 'SECTION', 'ARTICLE', 'NAV', 'FOOTER', 'HEADER']);
    return !NOISE_TAGS.has(el.tagName);
  }

  // ─── Layer 2: Per-field debounce state ───────────────────────────────────
  const DEBOUNCE_MS = 300;
  const debounceTimers = new WeakMap(); // el → timer id

  // ─── Layer 3: Focus-session tracking ─────────────────────────────────────
  // Kept alive past focusout so the debounce callback (firing after blur) can
  // still read the snapshot. Overwritten on the next focusin for the same el.
  const focusSessions = new WeakMap(); // el → { valueAtFocus, hadCopyPaste }
  let currentFocusEl = null;

  // ─── Label resolution (3 strategies) ────────────────────────────────────
  function findLabel(el) {
    // 1. Explicit <label for="id">
    if (el.id) {
      try {
        const lbl = document.querySelector('label[for="' + el.id.replace(/"/g, '\\"') + '"]');
        if (lbl) return lbl.textContent.trim();
      } catch (_) {}
    }
    // 2. Ancestor <label>
    const wrapping = el.closest('label');
    if (wrapping) {
      // Clone so we can strip the input's own value text
      const clone = wrapping.cloneNode(true);
      const inner = clone.querySelector('input, textarea, select');
      if (inner) inner.remove();
      return clone.textContent.trim();
    }
    // 3. Immediately preceding sibling element (common pattern: <label><span>Name</span><input>)
    let prev = el.previousElementSibling;
    if (prev && !FORM_FIELD_TAGS.has(prev.tagName)) return prev.textContent.trim();
    // 4. Parent's own text nodes (handles bare "Label: <input>" markup)
    const p = el.parentElement;
    if (p) {
      for (const node of p.childNodes) {
        if (node.nodeType === Node.TEXT_NODE) {
          const t = node.textContent.trim();
          if (t) return t;
        }
      }
    }
    return '';
  }

  // ─── Nearby text extraction ───────────────────────────────────────────────
  // Walks up the DOM and collects sibling text to give Gemini semantic context.
  function getNearbyText(el) {
    const seen = new Set();
    const results = [];
    let node = el.parentElement;
    let depth = 0;

    while (node && depth < 4 && results.length < 5) {
      for (const child of node.children) {
        if (child.contains(el)) continue; // skip the branch that contains our element
        const raw = (child.textContent || '').trim().replace(/\s+/g, ' ');
        if (raw && raw.length <= 80 && !seen.has(raw)) {
          seen.add(raw);
          results.push(raw);
        }
        if (results.length >= 5) break;
      }
      node = node.parentElement;
      depth++;
    }
    return results;
  }

  // ─── Context builder ──────────────────────────────────────────────────────
  function buildContext(el) {
    if (!el || !el.tagName) {
      return { id: null, name: null, type: null, placeholder: null, label: '', nearby_labels: [], url: window.location.href, timestamp: Date.now() };
    }
    return {
      id:            el.id          || null,
      name:          el.name        || null,
      type:          el.type        || el.tagName.toLowerCase(),
      placeholder:   el.placeholder || null,
      label:         findLabel(el),
      nearby_labels: getNearbyText(el),
      url:           window.location.href,
      timestamp:     Date.now()
    };
  }

  // ─── Emit to Python layer ─────────────────────────────────────────────────
  function emit(obj) {
    if (typeof window.__arcana_push__ === 'function') {
      try {
        window.__arcana_push__(obj);
      } catch (err) {
        // Swallow — never let an emit failure crash the page
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // EVENT LISTENERS (all capturing phase so we catch events on any sub-element)
  // ═══════════════════════════════════════════════════════════════════════════

  // ── copy ──────────────────────────────────────────────────────────────────
  // Two strategies so we capture both standard pages and rich web apps
  // (e.g. Google Sheets) that write to the clipboard programmatically
  // instead of using a text selection.
  document.addEventListener('copy', function () {
    const el = document.activeElement;

    if (currentFocusEl) {
      const s = focusSessions.get(currentFocusEl);
      if (s) s.hadCopyPaste = true;
    }

    // Strategy 1 — text selection (works for normal web pages, <span>, <td>, etc.)
    const selected = window.getSelection ? window.getSelection().toString().trim() : '';
    if (selected) {
      emit({ event: 'copy', value: selected, context: buildContext(el) });
      return;
    }

    // Strategy 2 — async clipboard read (Google Sheets sets the clipboard
    // programmatically; window.getSelection() is empty in that case).
    // We wait 80 ms so the app's own copy handler finishes writing first.
    if (navigator.clipboard && navigator.clipboard.readText) {
      var snapEl = el; // capture before the timeout fires
      setTimeout(function () {
        navigator.clipboard.readText().then(function (text) {
          text = (text || '').trim();
          if (text) emit({ event: 'copy', value: text, context: buildContext(snapEl) });
        }).catch(function () { /* clipboard-read permission unavailable — skip */ });
      }, 80);
    }
  }, true);

  // ── paste ─────────────────────────────────────────────────────────────────
  // Accepts standard form fields AND contenteditable elements (Google Sheets
  // edit cells use contenteditable divs, not <input> elements).
  function isPasteTarget(el) {
    if (!el || !el.tagName) return false;
    if (isFormField(el)) return true;
    if (el.isContentEditable) return true;
    var role = el.getAttribute && el.getAttribute('role');
    return role === 'textbox' || role === 'gridcell';
  }

  document.addEventListener('paste', function (e) {
    const el = document.activeElement;
    if (!isPasteTarget(el)) return;

    const value = ((e.clipboardData || window.clipboardData).getData('text') || '').trim();
    if (!value) return;

    const s = focusSessions.get(el);
    if (s) s.hadCopyPaste = true;

    emit({ event: 'paste', value: value, context: buildContext(el) });
  }, true);

  // ── input (debounced) ─────────────────────────────────────────────────────
  document.addEventListener('input', function (e) {
    const el = e.target;
    if (!isFormField(el)) return;                 // Layer 1
    if (el.tagName === 'SELECT') return;          // SELECT uses 'change' event

    if (debounceTimers.has(el)) clearTimeout(debounceTimers.get(el)); // Layer 2 reset

    const timer = setTimeout(function () {
      debounceTimers.delete(el);
      const value = el.value;
      if (!value.trim()) return;

      // Layer 3: discard if value is identical to what it was at focus time
      // and no copy/paste occurred (pure glance or accidental key press reverted)
      const s = focusSessions.get(el);
      if (s && s.valueAtFocus === value && !s.hadCopyPaste) return;

      emit({ event: 'input', value: value, context: buildContext(el) });
    }, DEBOUNCE_MS);

    debounceTimers.set(el, timer);
  }, true);

  // ── change (select dropdowns) ─────────────────────────────────────────────
  document.addEventListener('change', function (e) {
    const el = e.target;
    if (el.tagName !== 'SELECT') return;
    // No debounce needed — a selection is a single, discrete action
    emit({ event: 'change', value: el.value, context: buildContext(el) });
  }, true);

  // ── focusin ───────────────────────────────────────────────────────────────
  // Snapshot only — not emitted. Sets up Layer 3 state.
  document.addEventListener('focusin', function (e) {
    const el = e.target;
    if (!isFormField(el)) return;
    currentFocusEl = el;
    focusSessions.set(el, { valueAtFocus: el.value || '', hadCopyPaste: false });
  }, true);

  // ── focusout ──────────────────────────────────────────────────────────────
  // Just clears the current-element pointer. Session record is intentionally
  // kept in focusSessions so the debounce callback can still read it after blur.
  document.addEventListener('focusout', function (e) {
    if (e.target === currentFocusEl) currentFocusEl = null;
  }, true);

  // ── Ready signal ──────────────────────────────────────────────────────────
  // Confirms to Python that inject.js loaded and __arcana_push__ is wired up.
  emit({ event: 'inject_ready', value: window.location.href, context: buildContext(document.activeElement) });

})();
