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

  async function submit() {
    const text = input.value.trim();
    if (!text || state.pending) return;
    input.value = '';
    state.history.push({ role: 'user', text });
    state.pending = true;
    renderStream();

    const botIdx = state.history.push({ role: 'bot', text: '', chips: [], pending: true }) - 1;
    renderStream();

    try {
      const ask = window.xiaofAsk || stubAsk;
      const result = await ask({ query: text });
      state.history[botIdx] = {
        role: 'bot',
        text: result.text || '',
        chips: Array.isArray(result.chips) ? result.chips : [],
      };
    } catch (err) {
      state.history[botIdx] = {
        role: 'bot',
        text: '小F 暂时打不通，稍后再试。',
        chips: [],
        error: true,
      };
      console.error('[xiaof] ask failed', err);
    } finally {
      state.pending = false;
      renderStream();
    }
  }

  // Stub backend — real wiring lands when the thread-search API contract closes.
  async function stubAsk({ query }) {
    await new Promise(r => setTimeout(r, 300));
    return {
      text: `（开发占位）已收到「${query}」，等后端 thread 检索 API 接上。`,
      chips: [],
    };
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
          el.innerHTML = '<span class="xf-dots">···</span>';
        } else {
          el.textContent = msg.text;
          if (msg.chips && msg.chips.length) {
            el.appendChild(renderChips(msg.chips));
          }
        }
        stream.appendChild(el);
      }
    });
    stream.scrollTop = stream.scrollHeight;
  }

  // chips: [{ thread_id, post_id, title, squad, author, time, snippet, url }]
  function renderChips(chips) {
    const wrap = document.createElement('div');
    wrap.className = 'xf-chips';
    const VISIBLE = 5;
    const shown = chips.slice(0, VISIBLE);
    shown.forEach(c => wrap.appendChild(renderChip(c)));
    if (chips.length > VISIBLE) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'xf-chips-more';
      more.textContent = `还有 ${chips.length - VISIBLE} 条相关 thread →`;
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
