/* 小F · global agent — front-end skeleton (PRD v0.2 §9b + design v0.1 §9)
 *
 * Scope of this skeleton:
 *   - Portal-mounted bubble + panel attached to <body>
 *   - Native pointer-events drag, touch-action: none
 *   - Edge-snap on release (16px), per-user localStorage position
 *   - Panel four-state machine: Bubble | Panel-Default | Panel-Max | Dragging
 *   - ⌘K global toggle, Esc to close, "/" focuses input
 *   - Thread chip render component (data-driven, click → cross-thread deep-link)
 *   - Post anchor highlight via CSS animation + animationend cleanup
 *   - Backend wired to a stub `window.xiaofAsk` resolver; replace with real
 *     codex runtime + thread-search API in Phase 1 follow-up.
 *
 * NO react-dnd. NO setTimeout-based style mutation for the highlight.
 * Bubble and panel are sibling DOM nodes; panel re-renders never touch bubble.
 */
(function () {
  'use strict';

  if (window.__xiaofMounted) return;
  window.__xiaofMounted = true;

  const POS_KEY = 'openforge.xiaof.pos.v1';
  const SIZE = 60;
  const EDGE = 16;
  const PANEL_W = 380;
  const PANEL_H = 560;
  const PANEL_W_MAX = 880;
  const PANEL_GAP = 8;

  const state = {
    pos: loadPos(),                 // { side: 'right'|'left', y: number }
    panelOpen: false,
    panelMax: false,
    dragging: false,
    history: [],                    // [{ role: 'user'|'bot', text, chips? }]
    pending: false,
  };

  function loadPos() {
    try {
      const raw = JSON.parse(localStorage.getItem(POS_KEY) || 'null');
      if (raw && (raw.side === 'left' || raw.side === 'right') && typeof raw.y === 'number') {
        return raw;
      }
    } catch (_) { /* noop */ }
    return { side: 'right', y: Math.round(window.innerHeight * 0.65) };
  }

  function savePos() {
    try { localStorage.setItem(POS_KEY, JSON.stringify(state.pos)); } catch (_) { /* noop */ }
  }

  // --- DOM ---------------------------------------------------------------

  const bubble = document.createElement('div');
  bubble.id = 'xf-bubble';
  bubble.setAttribute('role', 'button');
  bubble.setAttribute('aria-label', '小F · 全局 agent');
  bubble.innerHTML = renderMark();
  document.body.appendChild(bubble);

  const panel = document.createElement('div');
  panel.id = 'xf-panel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', '小F');
  panel.innerHTML = `
    <div class="xf-header">
      <div class="xf-header-mark">${renderMark()}</div>
      <div class="xf-header-title">小F</div>
      <div class="xf-header-actions">
        <button type="button" data-act="clear" title="清空当前对话">⌫</button>
        <button type="button" data-act="max" title="最大化">⤢</button>
        <button type="button" data-act="min" title="最小化">⤓</button>
        <button type="button" data-act="close" title="关闭">×</button>
        <span class="xf-unread-badge" aria-hidden="true"></span>
      </div>
    </div>
    <div class="xf-stream" id="xf-stream"></div>
    <div class="xf-input-wrap">
      <textarea class="xf-input" id="xf-input" rows="1"
        placeholder="问点什么…" aria-label="向小F 发问"></textarea>
      <div class="xf-footer">小F 会检索你能看到的 thread · Enter 发送 · Shift+Enter 换行</div>
    </div>
  `;
  document.body.appendChild(panel);

  const stream = panel.querySelector('#xf-stream');
  const input = panel.querySelector('#xf-input');

  // --- Layout (bubble + panel positions) ---------------------------------

  function positionBubble() {
    const y = clamp(state.pos.y, EDGE, window.innerHeight - SIZE - EDGE);
    bubble.style.top = y + 'px';
    if (state.pos.side === 'right') {
      bubble.style.right = EDGE + 'px';
      bubble.style.left = 'auto';
    } else {
      bubble.style.left = EDGE + 'px';
      bubble.style.right = 'auto';
    }
  }

  function positionPanel() {
    const w = state.panelMax ? Math.min(PANEL_W_MAX, window.innerWidth - 160) : PANEL_W;
    const h = state.panelMax ? Math.round(window.innerHeight * 0.8) : PANEL_H;
    panel.style.width = w + 'px';
    panel.style.height = h + 'px';

    const top = clamp(state.pos.y, EDGE, window.innerHeight - h - EDGE);
    panel.style.top = top + 'px';
    panel.style.bottom = 'auto';

    if (state.pos.side === 'right') {
      panel.style.right = (EDGE + SIZE + PANEL_GAP) + 'px';
      panel.style.left = 'auto';
    } else {
      panel.style.left = (EDGE + SIZE + PANEL_GAP) + 'px';
      panel.style.right = 'auto';
    }
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  // --- Drag (native pointer events; no react-dnd) ------------------------

  let dragStart = null;
  let movedPx = 0;

  bubble.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    dragStart = { x: e.clientX, y: e.clientY, startY: state.pos.y };
    movedPx = 0;
    bubble.setPointerCapture(e.pointerId);
  });

  bubble.addEventListener('pointermove', (e) => {
    if (!dragStart) return;
    const dx = e.clientX - dragStart.x;
    const dy = e.clientY - dragStart.y;
    movedPx = Math.max(movedPx, Math.abs(dx) + Math.abs(dy));
    if (movedPx < 3) return;
    if (!state.dragging) {
      state.dragging = true;
      bubble.classList.add('is-dragging');
    }
    state.pos.y = clamp(dragStart.startY + dy, EDGE, window.innerHeight - SIZE - EDGE);
    // live horizontal: just track which half we're on, snap on release
    state.pos.side = (e.clientX > window.innerWidth / 2) ? 'right' : 'left';
    positionBubble();
    if (state.panelOpen) positionPanel();
  });

  bubble.addEventListener('pointerup', (e) => {
    try { bubble.releasePointerCapture(e.pointerId); } catch (_) {}
    const wasDragging = state.dragging;
    state.dragging = false;
    bubble.classList.remove('is-dragging');
    dragStart = null;
    if (wasDragging) {
      // Snap finalised already in pointermove; just persist + reposition.
      positionBubble();
      if (state.panelOpen) positionPanel();
      savePos();
    } else if (movedPx < 3) {
      togglePanel();
    }
  });

  bubble.addEventListener('pointercancel', () => {
    state.dragging = false;
    bubble.classList.remove('is-dragging');
    dragStart = null;
  });

  // --- Panel controls -----------------------------------------------------

  panel.querySelector('[data-act="close"]').addEventListener('click', closePanel);
  panel.querySelector('[data-act="min"]').addEventListener('click', closePanel);
  panel.querySelector('[data-act="max"]').addEventListener('click', () => {
    state.panelMax = !state.panelMax;
    panel.classList.toggle('is-max', state.panelMax);
    positionPanel();
  });
  panel.querySelector('[data-act="clear"]').addEventListener('click', () => {
    if (!state.history.length) return;
    if (confirm('清空当前对话？')) {
      state.history = [];
      renderStream();
    }
  });

  function openPanel() {
    state.panelOpen = true;
    panel.classList.add('is-open');
    positionPanel();
    renderStream();
    setTimeout(() => input.focus(), 0);
  }
  function closePanel() {
    state.panelOpen = false;
    panel.classList.remove('is-open');
  }
  function togglePanel() {
    if (state.panelOpen) closePanel(); else openPanel();
  }

  // --- Global keybindings -------------------------------------------------

  window.addEventListener('keydown', (e) => {
    const meta = e.metaKey || e.ctrlKey;
    if (meta && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      togglePanel();
      return;
    }
    if (e.key === 'Escape' && state.panelOpen) {
      e.preventDefault();
      closePanel();
      return;
    }
    if (e.key === '/' && state.panelOpen && document.activeElement !== input) {
      // Only intercept when panel is the active surface, never steal "/" globally.
      const tag = (document.activeElement?.tagName || '').toLowerCase();
      if (tag !== 'input' && tag !== 'textarea') {
        e.preventDefault();
        input.focus();
      }
    }
  });

  // --- Input handling -----------------------------------------------------

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  });

  // A-7.6 — error / empty / zero-result must be visually indistinguishable.
  // Any of the four contract errors (unauth | rate_limited | upstream_failed
  // | internal) and any genuine zero-result render this exact same string.
  const NO_MATCH_BODY = '没找到匹配的 thread。';

  async function submit() {
    const text = input.value.trim();
    if (!text || state.pending) return;
    input.value = '';
    state.history.push({ role: 'user', text });
    state.pending = true;

    // Push the bot message, render the whole stream ONCE so the bubble
    // DOM exists, then mutate only that bubble for token / chips updates.
    // This is the §9b rule: SSE token append must not re-render the world.
    const botIdx = state.history.push({ role: 'bot', text: '', chips: [], pending: true }) - 1;
    renderStream();
    const bubble = stream.lastElementChild;
    const textNode = document.createTextNode('');
    bubble.innerHTML = '';
    bubble.appendChild(textNode);
    const chipsHost = document.createElement('div');
    bubble.appendChild(chipsHost);

    const appendToken = (t) => {
      if (!t) return;
      state.history[botIdx].text += t;
      textNode.data = state.history[botIdx].text;
      stream.scrollTop = stream.scrollHeight;
    };
    const setChips = (chips, chipTotal) => {
      const list = Array.isArray(chips) ? chips : [];
      state.history[botIdx].chips = list;
      chipsHost.innerHTML = '';
      if (list.length) {
        chipsHost.appendChild(renderChips(list, chipTotal));
      }
    };
    const finalize = (failed) => {
      state.history[botIdx].pending = false;
      if (failed || !state.history[botIdx].text) {
        state.history[botIdx].text = NO_MATCH_BODY;
        textNode.data = NO_MATCH_BODY;
      }
      state.pending = false;
    };

    try {
      const ask = window.xiaofAsk || sseAsk;
      await ask({ query: text }, { onToken: appendToken, onChips: setChips });
      finalize(false);
    } catch (err) {
      // A-7.6: never branch on error kind; always the same body.
      console.warn('[xiaof] ask failed', err);
      finalize(true);
    }
  }

  // Default backend: POST /api/xiaof/ask, parse SSE per contract v0.1 §2.
  // Event order: meta → token* → chips → done. Any error event terminates
  // and is mapped uniformly to NO_MATCH_BODY by submit()'s catch.
  async function sseAsk(payload, hooks) {
    const body = JSON.stringify({
      query: payload.query,
      session_id: payload.session_id || sessionId(),
      client: { url: location.href, tz: tzGuess() },
    });
    const res = await fetch('/api/xiaof/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
      body,
      credentials: 'same-origin',
    });
    if (!res.ok || !res.body) {
      throw new Error('xiaof http ' + res.status);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    let sawError = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // Split on SSE frame separator (blank line).
      let idx;
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const parsed = parseSseFrame(frame);
        if (!parsed) continue;
        if (parsed.event === 'token') {
          hooks.onToken(parsed.data.text || '');
        } else if (parsed.event === 'chips') {
          hooks.onChips(parsed.data.chips, parsed.data.chip_total);
        } else if (parsed.event === 'error') {
          sawError = parsed.data.code || 'internal';
        }
        // meta / done are observed but require no UI hook in M1.
      }
    }
    if (sawError) throw new Error('xiaof error ' + sawError);
  }

  function parseSseFrame(frame) {
    const lines = frame.split(/\r?\n/);
    let event = 'message';
    const dataLines = [];
    for (const line of lines) {
      if (!line || line.startsWith(':')) continue;
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
    }
    if (!dataLines.length) return null;
    try { return { event, data: JSON.parse(dataLines.join('\n')) }; }
    catch (_) { return null; }
  }

  // Per-tab session id (D-2: no persistence across tabs / reloads).
  let _sid = null;
  function sessionId() {
    if (_sid) return _sid;
    _sid = 'xf-' + (crypto?.randomUUID?.() || Math.random().toString(36).slice(2));
    return _sid;
  }
  function tzGuess() {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; }
    catch (_) { return ''; }
  }

  // --- Render -------------------------------------------------------------

  function renderStream() {
    if (!state.history.length) {
      stream.innerHTML = `
        <div class="xf-empty">
          我能做什么？
          <div class="xf-empty-examples">
            <button type="button" class="xf-empty-example" data-fill="现在 Asia/Shanghai 几点？">现在 Asia/Shanghai 几点？</button>
            <button type="button" class="xf-empty-example" data-fill="上次 alice 说 hero 高度的决策在哪个 thread？">上次 alice 说 hero 高度的决策在哪个 thread？</button>
            <button type="button" class="xf-empty-example" data-fill="sherry 之前发的悬浮气泡截图">sherry 之前发的悬浮气泡截图</button>
          </div>
        </div>`;
      stream.querySelectorAll('.xf-empty-example').forEach(btn => {
        btn.addEventListener('click', () => {
          input.value = btn.dataset.fill;
          input.focus();
        });
      });
      return;
    }
    stream.innerHTML = '';
    state.history.forEach(msg => {
      if (msg.role === 'user') {
        const el = document.createElement('div');
        el.className = 'xf-msg-user';
        el.textContent = msg.text;
        stream.appendChild(el);
      } else {
        const el = document.createElement('div');
        el.className = 'xf-msg-bot';
        if (msg.pending) {
          // submit() takes over this node after the initial render — see
          // appendToken / setChips closures. We leave it empty so the
          // first incoming token appears immediately without a flash.
          el.textContent = '';
        } else {
          el.textContent = msg.text;
          if (msg.chips && msg.chips.length) {
            el.appendChild(renderChips(msg.chips, msg.chips.length));
          }
        }
        stream.appendChild(el);
      }
    });
    stream.scrollTop = stream.scrollHeight;
  }

  // chips: [{ thread_id, post_id, title, squad, author, time, snippet, url }]
  // chipTotal comes from the server `done.chip_total` (>= chips.length).
  // Per contract §2: backend caps the wire to 5; we render those 5 and
  // surface the overflow count from chipTotal rather than chips.length.
  function renderChips(chips, chipTotal) {
    const wrap = document.createElement('div');
    wrap.className = 'xf-chips';
    const VISIBLE = 5;
    const shown = chips.slice(0, VISIBLE);
    shown.forEach(c => wrap.appendChild(renderChip(c)));
    const total = (typeof chipTotal === 'number' && chipTotal > chips.length) ? chipTotal : chips.length;
    if (total > VISIBLE) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'xf-chips-more';
      more.textContent = `还有 ${total - VISIBLE} 条相关 thread →`;
      more.addEventListener('click', () => {
        more.remove();
        chips.slice(VISIBLE).forEach(c => wrap.appendChild(renderChip(c)));
      });
      wrap.appendChild(more);
    }
    return wrap;
  }

  function renderChip(c) {
    const a = document.createElement('a');
    a.className = 'xf-chip';
    a.href = c.url || ('#post-' + c.post_id);
    a.innerHTML = `
      <div class="xf-chip-title">📎 ${escapeHtml(c.title || '(无标题)')}</div>
      <div class="xf-chip-meta">${escapeHtml(c.squad || '')} · ${escapeHtml(c.author || '')} · ${escapeHtml(c.time || '')}</div>
      <div class="xf-chip-snippet">${escapeHtml(c.snippet || '')}</div>
      <div class="xf-chip-open">↗</div>
    `;
    a.addEventListener('click', (e) => {
      // SPA navigation hook: prefer in-app router if exposed.
      if (window.xiaofNavigate && c.thread_id) {
        e.preventDefault();
        window.xiaofNavigate({ thread_id: c.thread_id, post_id: c.post_id });
        // Force hash re-trigger so same-post reclick replays the highlight.
        forceAnchorHash(c.post_id);
      } else if (c.post_id) {
        // No router hook → still re-trigger hash for highlight replay.
        e.preventDefault();
        forceAnchorHash(c.post_id);
      }
    });
    return a;
  }

  function forceAnchorHash(postId) {
    if (!postId) return;
    // Reset hash first so React Router / hashchange listeners always fire,
    // even if user clicks the same chip twice in a row.
    history.replaceState(null, '', '#');
    location.hash = 'post-' + postId;
  }

  // --- Post anchor highlight (CSS-only, PRD §4.2) -------------------------
  //
  // When location.hash matches `#post-<id>`, find the post element and add
  // `is-anchored`. The CSS animation runs once; we remove the class on
  // animationend so the next chip click can replay it.
  function applyAnchorFromHash() {
    const m = (location.hash || '').match(/^#post-([A-Za-z0-9._-]+)/);
    if (!m) return;
    const postId = m[1];
    // Wait one frame so the target post (which may have just mounted via
    // router navigation) is in the DOM.
    requestAnimationFrame(() => {
      const target = document.querySelector('.post[data-post-id="' + cssEscape(postId) + '"]');
      if (!target) return;
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.classList.remove('is-anchored');
      void target.offsetWidth;          // restart animation
      target.classList.add('is-anchored');
    });
  }
  window.addEventListener('hashchange', applyAnchorFromHash);
  // Auto-cleanup: animationend removes class, so next playback works.
  document.addEventListener('animationend', (e) => {
    if (e.animationName === 'xf-post-anchor' && e.target instanceof Element) {
      e.target.classList.remove('is-anchored');
    }
  });

  // --- Resize / first paint ----------------------------------------------

  window.addEventListener('resize', () => {
    positionBubble();
    if (state.panelOpen) positionPanel();
  });

  positionBubble();
  applyAnchorFromHash();

  // --- Utilities ----------------------------------------------------------

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function cssEscape(s) {
    return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  }

  // Geometric mark — "桥/连接" per design §2. Two squares offset diagonally,
  // linked by a stroke. Deliberately non-anthropomorphic (D-5).
  function renderMark() {
    return `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <rect x="3" y="3" width="8" height="8" rx="2" fill="currentColor" opacity="0.85"/>
      <rect x="13" y="13" width="8" height="8" rx="2" fill="currentColor" opacity="0.85"/>
      <path d="M9 9 L15 15" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
    </svg>`;
  }

  // Public hooks for the integrator
  window.xiaof = {
    open: openPanel,
    close: closePanel,
    toggle: togglePanel,
    setBackend(fn) { window.xiaofAsk = fn; },
    setNavigator(fn) { window.xiaofNavigate = fn; },
    _state: state,
  };
})();
