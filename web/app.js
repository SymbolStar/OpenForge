// OpenForge web app — squad / thread / posts, vanilla JS, Slack-shaped.

// Legacy hardcoded list — kept ONLY as fallback for AGENT_COLOR_CLASS (avatar
// colours) and as a last-resort fallback if /api/employees is unreachable.
// The squad-modal member list is built dynamically from /api/employees
// (curated roster: agents with workspace-<id>/SOUL.md). See buildMemberControls.
const AGENTS = ['milk', 'sentry', 'bugfix', 'milly', 'kb'];
const POLL_MS = 8000;
let showArchivedSquads = false;

const els = {
  // squad rail
  squadList: document.getElementById('squad-list'),
  btnNewSquad: document.getElementById('btn-new-squad'),
  statusDot: document.getElementById('status-dot'),
  statusText: document.getElementById('status-text'),

  // thread rail
  squadTitle: document.getElementById('squad-title'),
  squadDescription: document.getElementById('squad-description'),
  squadProjectChip: document.getElementById('squad-project-chip'),
  threadList: document.getElementById('thread-list'),
  btnRefreshThreads: document.getElementById('btn-refresh-threads'),
  threadComposerInput: document.getElementById('thread-composer-input'),
  threadComposerCount: document.getElementById('thread-composer-count'),

  // detail pane
  detailTitle: document.getElementById('detail-title'),
  detailSub: document.getElementById('detail-sub'),
  detailStatus: document.getElementById('detail-status'),
  detailParticipants: document.getElementById('detail-participants'),
  btnCloseThread: document.getElementById('btn-close-thread'),
  btnRefreshDetail: document.getElementById('btn-refresh-detail'),
  postList: document.getElementById('post-list'),
  pinnedArea: document.getElementById('pinned-area'),
  postComposerInput: document.getElementById('post-composer-input'),
  btnSendPost: document.getElementById('btn-send-post'),

  // modal
  modal: document.getElementById('squad-modal'),
  form: document.getElementById('squad-form'),
  btnCloseModal: document.getElementById('btn-close-modal'),
  btnCancelModal: document.getElementById('btn-cancel-modal'),
  btnDeleteSquad: document.getElementById('btn-delete-squad'),
  btnSubmitSquad: document.getElementById('btn-submit-squad'),
  btnEditSquad: document.getElementById('btn-edit-squad'),
  modalTitle: document.getElementById('modal-title'),
  memberCheckboxes: document.getElementById('member-checkboxes'),
  chairSelect: document.getElementById('chair-select'),
  // settings + reply nesting
  btnSettings: document.getElementById('btn-settings'),
  settingsModal: document.getElementById('settings-modal'),
  settingsForm: document.getElementById('settings-form'),
  btnCloseSettings: document.getElementById('btn-close-settings'),
  btnCloseSettings2: document.getElementById('btn-close-settings-2'),
  composerReplyBanner: document.getElementById('composer-reply-banner'),
  // (btnCancelReply removed in V1.1 — cancel × lives inside renderQuoteCard)
};

// ─── settings (localStorage) ─────────────────────────────────
const SETTINGS_KEY = 'openforge.settings.v1';
// `replyNesting` was removed in V1.1 (inline quote-card UI replaced tree
// nesting). Default kept here only so old localStorage payloads don't barf.
const SETTINGS_DEFAULTS = { myAvatar: '', myAvatarColor: '' };
function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    return { ...SETTINGS_DEFAULTS, ...saved };
  } catch { return { ...SETTINGS_DEFAULTS }; }
}
function saveSettings(s) {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch {}
}

const state = {
  squads: [],
  squadDetails: new Map(),  // squad_id -> { squad, threads }
  currentSquadId: null,
  currentThreadId: null,
  currentThread: null,
  pollTimer: null,
  threadEventSource: null,  // EventSource for the currently-open thread
  threadEventThreadId: null,
  settings: loadSettings(),
  replyTo: null,  // { post_id, speaker, content } when composing a reply
};
// v0.5+: expose state + the squad/thread helpers so the Activity IIFE can
// soft-sync currentSquadId when a cross-squad thread is selected (alice's
// edge case: detail-shown thread should also be selected in Threads view).
window.state = state;

const MENTION_RE = /@([\w\-\u4e00-\u9fff]+)/g;
const AGENT_COLOR_CLASS = new Map(AGENTS.map(a => [a, `av-${a}`]));

// ─── utils ────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function setStatus(text, ok = true) {
  els.statusText.textContent = text;
  els.statusDot.className = 'dot ' + (ok ? 'dot-ok' : 'dot-warn');
}

// PRD v1.2 follow-up (judy review #2): observable v0.7 chip usage.
// Best-effort POST; failures are swallowed (telemetry must never block
// render). Debounced per source so one re-render burst doesn't inflate
// the counter — we want "a v0.7 chip was visible to the user" semantics.
const _v07RecentlySent = new Set();
function v07Bump(source) {
  try {
    const key = source || 'chip';
    if (_v07RecentlySent.has(key)) return;
    _v07RecentlySent.add(key);
    setTimeout(() => _v07RecentlySent.delete(key), 1500);
    fetch('/api/v07-chip-hits/bump', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: key }),
      keepalive: true,
    }).catch(() => {});
  } catch (_) { /* never throw from telemetry */ }
}

function showToast(msg, ms = 2000) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => { t.hidden = true; }, ms);
}

function avatarLabel(name) {
  if ((name || '').toLowerCase() === 'scott') {
    const a = (state.settings.myAvatar || '').trim();
    if (a) return [...a].slice(0, 2).join('');
  }
  // V1.2: prefer the IDENTITY.md display name's first char (so 'designer'
  // shows 'D' for Dora) and fall back to the raw id when no display
  // name is known.
  const src = displayName(name) || name || '?';
  return [...src][0].toUpperCase();
}

function agentEmoji(agentId) {
  return _identityEmoji.get(agentId) || '';
}

function defaultAvatar(agentId) {
  const api = window.OpenForgeAvatar;
  if (api && typeof api.getDefaultAvatar === 'function') {
    return api.getDefaultAvatar(agentId, agentEmoji(agentId));
  }
  return { pngPath: '', glyph: avatarLabel(agentId), key: 'fallback' };
}

function renderDefaultAvatarInner(agentId) {
  const av = defaultAvatar(agentId);
  const img = av.pngPath
    ? `<img class="avatar-img" src="${escapeAttr(av.pngPath)}" alt="" aria-hidden="true" loading="lazy" />`
    : '';
  return `${img}<span class="avatar-glyph">${escapeHtml(av.glyph || avatarLabel(agentId))}</span>`;
}

// ─── PR-3 / PRD-v1.0 §4: employee-avatar deep-link to agent webchat ───
// Boot-time fetch of /api/config caches the webchat base URL; /api/employees
// caches the employee roster. renderAvatarTag() then emits a clickable <a>
// for any post.speaker that is an employee, or a plain <div> otherwise
// (scott / __router__ / unknown ids stay non-interactive).
let _webchatBase = 'http://127.0.0.1:18789';
let _employeeSet = new Set();
// V1.2: agent_id → display name from IDENTITY.md (e.g. 'designer' → 'Dora').
// Filled by loadEmployeeSet() via /api/employees?with_identity=1. Display
// helpers consult this map; storage keys (post.speaker, squad.members,
// avatar colour class) stay on agent_id.
let _displayNames = new Map();
let _identityEmoji = new Map();
// Reverse map for the @-picker / future inline autocomplete:
// 'dora' → 'designer', 'xiaoba' → 'xiaoba' (back-mapped from display tokens).
let _displayToId = new Map();

async function loadWebchatBase() {
  try {
    const res = await fetch('/api/config');
    if (res.ok) {
      const cfg = await res.json();
      if (cfg && typeof cfg.webchat_base_url === 'string' && cfg.webchat_base_url) {
        _webchatBase = cfg.webchat_base_url.replace(/\/$/, '');
      }
      if (cfg && typeof cfg.version === 'string' && cfg.version) {
        const el = document.getElementById('settings-version');
        if (el) el.textContent = cfg.version;
      }
    }
  } catch (e) {
    // Network error → keep the hardcoded fallback so rendering isn't blocked.
  }
}

async function loadEmployeeSet() {
  try {
    // V1.2: ask for the enriched form so we get IDENTITY.md names too.
    const res = await fetch('/api/employees?with_identity=1');
    if (res.ok) {
      const list = await res.json();
      if (Array.isArray(list)) {
        _employeeSet = new Set();
        _displayNames = new Map();
        _identityEmoji = new Map();
        _displayToId = new Map();
        list.forEach(item => {
          // Back-compat: server may still return bare strings if the
          // ?with_identity flag is dropped or the endpoint is older.
          if (typeof item === 'string' && item) {
            _employeeSet.add(item);
            return;
          }
          if (!item || typeof item !== 'object') return;
          const id = item.id;
          if (!id) return;
          _employeeSet.add(id);
          const name = (item.name || '').trim();
          if (name && name !== id) {
            _displayNames.set(id, name);
            // Build alias → id reverse map. Compound names like
            // '小巴 (Xiaoba / Buffett)' produce three aliases; the head
            // token wins on the conflict (same precedence the backend
            // uses for resolution).
            const tokens = name
              .replace(/[()]/g, ' ')
              .split(/[\s,/&;]+/)
              .map(t => t.trim())
              .filter(Boolean);
            tokens.forEach(t => {
              const k = t.toLowerCase();
              if (!_displayToId.has(k)) _displayToId.set(k, id);
            });
          }
          const emoji = (item.emoji || '').trim();
          if (emoji) _identityEmoji.set(id, emoji);
          // Also map id → id so 'designer' still resolves.
          _displayToId.set(id.toLowerCase(), id);
        });
      }
    }
  } catch (e) {
    // Empty maps are safe: every avatar will just render as a plain div
    // and display names will fall back to agent ids.
  }
}

// V1.2: display-friendly name for an agent id.
// Falls back to the id itself for unknown / non-employee speakers
// (scott, __router__, runtime profiles), so call-sites never have to
// special-case 'is this name resolvable?'.
function displayName(agentId) {
  if (!agentId) return '';
  return _displayNames.get(agentId) || agentId;
}

function isEmployee(name) {
  return !!name && _employeeSet.has(name);
}

// Build the webchat deep-link for an agent avatar. With a thread_id, we
// link to the per-thread explicit session the post-router actually spawns
// (`agent:<id>:explicit:forge-<thread_id>-<id>`), so clicking the avatar
// lands in the same conversation that produced this thread's posts.
// Without a thread_id, we fall back to the agent's `main` session (used
// outside any thread context).
//
// The format must match what post_router constructs when calling
// `openclaw agent` — if you change one side, change the other and bump
// the V1.x note in docs/PRD-v1.0-thread-collaboration.md §4.3.
function webchatLinkFor(agentId, threadId) {
  const id = encodeURIComponent(agentId);
  if (threadId) {
    // No encoding on threadId or session segment: openclaw session ids
    // never contain reserved URL chars (validated by SQUAD_ROUTE_RE-style
    // gates) and the webchat parser splits on `:` so keeping it raw
    // matches what users see in chat history shares.
    return `${_webchatBase}/chat?session=agent:${id}:explicit:forge-${threadId}-${id}`;
  }
  return `${_webchatBase}/chat?session=agent:${id}:main`;
}

function renderAvatarTag(name, { extraClass = '', styleAttr = '', threadId = null } = {}) {
  const cls = `avatar avatar-default ${avatarClass(name)}${extraClass ? ' ' + extraClass : ''}`;
  const inner = renderDefaultAvatarInner(name);
  const friendly = displayName(name);
  if (isEmployee(name)) {
    const href = webchatLinkFor(name, threadId);
    const title = threadId
      ? `点击查看 ${friendly} 在本 thread 的 session`
      : `点击查看 ${friendly} 的 main session`;
    return `<a class="${cls} avatar-link" href="${href}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(title)}"${styleAttr}>${inner}</a>`;
  }
  return `<div class="${cls}"${styleAttr} title="${escapeHtml(friendly || '')}">${inner}</div>`;
}

function avatarClass(name) {
  if ((name || '').toLowerCase() === 'scott' && (state.settings.myAvatarColor || '').trim()) {
    return 'av-custom';
  }
  return AGENT_COLOR_CLASS.get(name) || 'av-default';
}

function avatarStyle(name) {
  if ((name || '').toLowerCase() === 'scott') {
    const c = (state.settings.myAvatarColor || '').trim();
    if (c) return ` style="background:${escapeAttr(c)};"`;
  }
  return '';
}

function escapeAttr(s) {
  return String(s).replace(/["<>&]/g, c => ({'"':'&quot;','<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
}

// v0.7/v0.8: file linking chip syntax — supports:
//   [[name.md]]                 → v0.7 default-root file (refs fallback)
//   [[root/name.md]]            → v0.7 explicit root  OR  v0.8 agent/label
//   [[ref:ref_abc123]]          → v0.8 explicit ref id
//   any of the above with |display label
const FILE_LINK_RE = /\[\[([A-Za-z0-9_.\-\/:]+)(?:\|([^\]]+))?\]\]/g;

// Rewrite <a href="http(s)://..."> to open in a new tab. Skip tags that
// already declare target=. Internal hash routes (#/...) stay in-tab so the
// SPA can handle them. Used by renderBody AND by every markdown preview
// (file ref preview, README pane, status card) so external links never
// blow the current SPA tab away.
function openExternalLinksInNewTab(html) {
  if (!html || typeof html !== 'string') return html;
  return html.replace(/<a\s+([^>]*?)href=("|')(https?:\/\/[^"']+)\2([^>]*)>/gi,
    (match, pre, q, href, post) => {
      if (/\btarget\s*=/i.test(pre) || /\btarget\s*=/i.test(post)) return match;
      return `<a ${pre}href=${q}${href}${q}${post} target="_blank" rel="noopener noreferrer">`;
    });
}

// Async ref index used by chip renderer + References tab.
window._forgeRefs = window._forgeRefs || { byId: new Map(), all: [], loaded: false, loading: null };

// Stale-cache recovery: when a chip resolver misses an unresolved [[…]]
// target, a new ref may have been registered after our last /api/refs
// fetch (e.g. designer just attached screenshots while this tab was open).
// Schedule a single background refresh + re-render of the current thread.
// Debounced + per-target deduped so a post with 6 unresolved chips only
// triggers ONE refetch, and we don't busy-loop if the ref truly doesn't
// exist server-side.
window._forgeRefsRefreshPending = window._forgeRefsRefreshPending || false;
window._forgeRefsMissTargets = window._forgeRefsMissTargets || new Set();
function scheduleRefIndexRefresh(target) {
  // Don't keep retrying a target we already failed to resolve after a fresh
  // fetch — that means the server really doesn't have it.
  if (window._forgeRefsMissTargets.has(target)) return;
  if (window._forgeRefsRefreshPending) return;
  window._forgeRefsRefreshPending = true;
  setTimeout(async () => {
    const before = (window._forgeRefs && window._forgeRefs.all.length) || 0;
    try {
      await loadRefIndex(true);
    } catch (_e) {
      /* ignore — keep existing cache */
    }
    window._forgeRefsRefreshPending = false;
    const idx = window._forgeRefs;
    const after = (idx && idx.all.length) || 0;
    // Re-resolve all currently-missing targets; anything still missing gets
    // permanently parked so we don't refetch on every render pass.
    for (const t of Array.from(window._forgeRefsMissTargets)) {
      if (resolveChipFromRefs(t)) {
        window._forgeRefsMissTargets.delete(t);
      }
    }
    if (!resolveChipFromRefs(target)) {
      window._forgeRefsMissTargets.add(target);
    }
    // Only repaint when the cache actually changed; avoids needless flicker.
    if (after !== before && typeof refreshCurrentThread === 'function') {
      refreshCurrentThread();
    }
  }, 150);
}

async function loadRefIndex(force) {
  const idx = window._forgeRefs;
  if (idx.loaded && !force) return idx;
  if (idx.loading && !force) return idx.loading;
  idx.loading = (async () => {
    try {
      const r = await fetch('/api/refs');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      const all = Array.isArray(data.refs) ? data.refs : [];
      idx.all = all;
      idx.byId = new Map(all.map(x => [x.id, x]));
      idx.loaded = true;
      return idx;
    } catch (e) {
      idx.all = [];
      idx.byId = new Map();
      idx.loaded = true;
      return idx;
    } finally {
      idx.loading = null;
    }
  })();
  return idx.loading;
}

// Synchronous lookup against cached refs (used during HTML render).
function resolveChipFromRefs(target) {
  const idx = window._forgeRefs;
  if (!idx || !idx.loaded) return null;
  // [[ref:id]]
  if (target.startsWith('ref:')) {
    return idx.byId.get(target.slice(4)) || null;
  }
  const slash = target.indexOf('/');
  if (slash > 0) {
    const agent = target.slice(0, slash);
    const label = target.slice(slash + 1);
    const hit = idx.all.find(r => r.source_agent === agent && r.label === label);
    if (hit) return hit;
    // also try label-only match in case agent segment was actually a root id
    const byLabel = idx.all.filter(r => r.label === label);
    if (byLabel.length === 1) return byLabel[0];
    return null;
  }
  // bare label — only unambiguous match wins
  const byLabel = idx.all.filter(r => r.label === target);
  if (byLabel.length === 1) return byLabel[0];
  return null;
}

// Markdown image syntax `![alt](url)` — only `/api/uploads/<file>` URLs are
// trusted and rendered as <img>; everything else falls through as plain text
// (XSS hardening).
const IMAGE_MD_RE = /!\[([^\]]*)\]\((\/api\/uploads\/[A-Za-z0-9._-]+)\)/g;

function renderBody(text) {
  // Replace [[file]] tokens AND ![](url) image tokens with sentinel
  // placeholders BEFORE escaping, so chip/img HTML survives escapeHtml().
  // Sentinels use \u0001 markers.
  const chips = [];
  const images = [];
  let piped = (text || '').replace(IMAGE_MD_RE, (_, alt, url) => {
    images.push({ url, alt });
    return `\u0001IMG${images.length - 1}\u0001`;
  });
  piped = piped.replace(FILE_LINK_RE, (_, target, label) => {
    // v0.8: try refs registry first (sync lookup against cached index)
    const refHit = resolveChipFromRefs(target);
    if (refHit) {
      chips.push({ kind: 'ref', ref: refHit, display: (label || refHit.label).trim(), target });
      return `\u0001CHIP${chips.length - 1}\u0001`;
    }
    // v0.7 fallback: [[name.md]] or [[root/name.md]]
    if (!/\.md$/i.test(target.split('/').pop() || '')) {
      // unresolved + not even a v0.7-shaped path → register as a miss and
      // kick off a background ref-index refresh; the post may be referring
      // to a ref registered AFTER we last fetched /api/refs (common when a
      // peer agent attaches files while this tab is open).
      scheduleRefIndexRefresh(target);
      chips.push({ kind: 'unresolved', display: (label || target).trim(), target });
      return `\u0001CHIP${chips.length - 1}\u0001`;
    }
    let root = '';
    let name = target;
    const slash = target.indexOf('/');
    if (slash > 0) {
      root = target.slice(0, slash);
      name = target.slice(slash + 1);
    }
    const display = (label || name).trim();
    chips.push({ kind: 'workspace', root, name, display, target });
    // PRD v1.2 follow-up (judy review #2): a v0.7 [[root/name.md]] chip
    // actually rendered to a user. This is the cleanest signal of v0.7
    // exposure (back-end forge_files.read_file was caught firing for
    // unrelated callers). Fire-and-forget; never blocks render.
    try { v07Bump('chip_workspace'); } catch (_) { /* no-op */ }
    return `\u0001CHIP${chips.length - 1}\u0001`;
  });
  // Render markdown (bold/italic/lists/headings/links/code…) via marked when
  // available; marked does its own HTML-escaping so we don't pre-escape. The
  // \u0001 sentinels above are control chars and survive untouched as text.
  let html;
  if (typeof marked !== 'undefined' && marked.parse) {
    try {
      html = marked.parse(piped, {
        breaks: true,    // single \n -> <br>, matches old pre-wrap feel
        gfm: true,
        mangle: false,
        headerIds: false,
      });
    } catch (_e) {
      html = escapeHtml(piped);
    }
  } else {
    html = escapeHtml(piped).replace(/`([^`\n]+)`/g,
      (_, code) => `<code>${escapeHtml(code)}</code>`);
  }
  html = openExternalLinksInNewTab(html);
  // mentions: run AFTER marked so we match plain text occurrences of @name.
  html = html.replace(MENTION_RE,
    (_, name) => `<span class="mention">@${escapeHtml(name)}</span>`);
  html = html.replace(/\u0001CHIP(\d+)\u0001/g, (_, idx) => {
    const c = chips[Number(idx)];
    if (!c) return '';
    if (c.kind === 'ref') {
      const href = `#/files/refs/${encodeURIComponent(c.ref.id)}`;
      const agentTag = c.ref.source_agent ? ` <span class="file-chip-agent">${escapeHtml(c.ref.source_agent)}</span>` : '';
      const absPath = c.ref.abs_path || '';
      const fav = absPath && window._forgeFavSet && window._forgeFavSet.has(absPath);
      const favBtn = absPath
        ? `<button type="button" class="file-chip-fav${fav ? ' is-favorited' : ''}" data-fav-toggle="1" data-fav-abs="${escapeAttr(absPath)}" data-fav-ref="${escapeAttr(c.ref.id)}" data-fav-agent="${escapeAttr(c.ref.source_agent || '')}" data-fav-thread="${escapeAttr(c.ref.thread_id || '')}" aria-pressed="${fav ? 'true' : 'false'}" title="${fav ? '取消收藏' : '收藏'} ${escapeAttr(c.ref.label)}" aria-label="${fav ? '取消收藏' : '收藏'} ${escapeAttr(c.ref.label)}">${fav ? '★' : '☆'}</button>`
        : '';
      return `<span class="file-chip-wrap"><a class="file-chip file-chip-ref" href="${href}" title="${escapeAttr(c.ref.label + ' · ' + (c.ref.source_agent || ''))}" data-file-chip="1" data-ref-id="${escapeAttr(c.ref.id)}">`
        + `<span class="file-chip-icon">📄</span>${escapeHtml(c.display)}${agentTag}</a>${favBtn}</span>`;
    }
    if (c.kind === 'unresolved') {
      return `<span class="file-chip file-chip-missing" title="未注册的引用: ${escapeAttr(c.target)}">`
        + `<span class="file-chip-icon">⚠️</span>${escapeHtml(c.display)}</span>`;
    }
    const hashRoot = c.root || '';
    const href = hashRoot
      ? `#/files/${encodeURIComponent(hashRoot)}/${encodeURIComponent(c.name)}`
      : `#/files/${encodeURIComponent(c.name)}`;
    return `<a class="file-chip" href="${href}" title="打开 ${escapeAttr(c.target)}" data-file-chip="1">`
      + `<span class="file-chip-icon">📄</span>${escapeHtml(c.display)}</a>`;
  });
  html = html.replace(/\u0001IMG(\d+)\u0001/g, (_, idx) => {
    const im = images[Number(idx)];
    if (!im) return '';
    return `<a class="post-image-link" href="${escapeAttr(im.url)}" target="_blank" rel="noopener">`
      + `<img class="post-image" src="${escapeAttr(im.url)}" alt="${escapeAttr(im.alt || 'image')}" loading="lazy" /></a>`;
  });
  return html;
}

function formatRelative(ts) {
  if (!ts) return '';
  const t = new Date(ts).getTime();
  if (!Number.isFinite(t)) return ts;
  const diff = Math.max(0, Date.now() - t);
  const s = Math.floor(diff / 1000);
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(ts).toLocaleDateString();
}

function autosize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 200) + 'px';
}

async function apiJson(url, options) {
  // Mark every mutating request from the UI so the server's speaker
  // spoofing guard can tell scott-from-UI apart from scott-from-curl.
  // (Agents talking to the loopback API never set this header, which is
  // exactly the point — server refuses speaker="scott" without it.)
  const opts = { ...(options || {}) };
  const method = (opts.method || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD') {
    opts.headers = { ...(opts.headers || {}), 'X-OpenForge-UI': '1' };
  }
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ─── unread tracking (client-side, localStorage) ──────────────────────
// We persist `lastSeenAt` per thread_id in localStorage. A thread is
// "unread" when its `last_post_at` > the cached `lastSeenAt`. New threads
// (never opened) count as unread iff they have any post. Marking-seen
// happens on selectThread and on every SSE refresh of the *currently
// open* thread (user is looking at it right now).
const LAST_SEEN_KEY = 'openforge:lastSeen:v1';

// Backend ships `last_post_at` as an ISO-8601 *string* (forge_store.py:
// `model["last_post_at"] = last_post["ts"]`). Some call sites pass
// `Date.now()` ms when no better timestamp is available. Normalize every
// timestamp through this helper before storing or comparing — mixing
// strings and numbers makes `>` do lexicographic compare and breaks
// unread completely (every thread looks unread forever, badge never
// drops to 0). 2026-05-28 bug found by scott.
function _normTs(v) {
  if (v == null) return 0;
  if (typeof v === 'number') return v > 0 ? v : 0;
  if (typeof v === 'string') {
    const ms = Date.parse(v);
    return Number.isFinite(ms) ? ms : 0;
  }
  return 0;
}

let _lastSeen = (() => {
  try {
    const raw = localStorage.getItem(LAST_SEEN_KEY);
    if (!raw) return {};
    const obj = JSON.parse(raw);
    if (!obj || typeof obj !== 'object') return {};
    // Migrate any legacy non-number entries (string/ISO) to ms so the
    // comparison in isThreadUnread always sees number-vs-number.
    const out = {};
    for (const [k, v] of Object.entries(obj)) {
      const n = _normTs(v);
      if (n > 0) out[k] = n;
    }
    return out;
  } catch { return {}; }
})();

function _persistLastSeen() {
  try { localStorage.setItem(LAST_SEEN_KEY, JSON.stringify(_lastSeen)); } catch {}
}

function markThreadSeen(threadId, at) {
  if (!threadId) return;
  const ts = _normTs(at) || Date.now();
  const prev = _lastSeen[threadId] || 0;
  if (ts > prev) {
    _lastSeen[threadId] = ts;
    _persistLastSeen();
  }
}

function isThreadUnread(t) {
  if (!t || !t.thread_id) return false;
  // 正在看这个 thread → 从不计未读。避免 SSE 推新 post 进 currentThread
  // 时、refreshThreadsForCurrentSquad 与 markThreadSeen 之间的 race 让
  // squad badge 反复点亮。同时保证“打开后立刻减 1 / 为 0 时消失”。
  if (t.thread_id === state.currentThreadId) return false;
  const lp = _normTs(t.last_post_at);
  if (!lp) return false;
  const seen = _lastSeen[t.thread_id] || 0;
  return lp > seen;
}

function squadUnreadCount(squadId) {
  const detail = state.squadDetails.get(squadId);
  const threads = detail?.threads || [];
  let n = 0;
  for (const t of threads) if (isThreadUnread(t)) n++;
  return n;
}

// dora's #3: tab 不在前台时让 document.title 打标
// 总未读数 = 跨所有 squad 的 unread thread 总和。不需要 favicon、不闪烁。
const BASE_TITLE = 'OpenForge';
function totalUnread() {
  let n = 0;
  for (const s of state.squads) n += squadUnreadCount(s.id);
  return n;
}
function updateUnreadTitle() {
  const n = totalUnread();
  document.title = n > 0 ? `(${n}) ${BASE_TITLE}` : BASE_TITLE;
  maybeNotifyNewUnread();
}

// ─── OS notifications ─────────────────────────────────────────────────
// Fire a Web Notification when a thread transitions from "read" → "unread"
// (a new post landed in a thread you haven't caught up on). Guards:
//   - permission granted (Settings toggle asks for it)
//   - tab is hidden — visible tabs already show the red dot
//   - notifications setting enabled
//   - never notify for the currently-open thread
//   - de-dupe via Notification tag on thread_id
// Body: "<squad emoji+name> · <thread title>".
const NOTIFY_PREFS_KEY = 'openforge.notifyPrefs.v1';
function loadNotifyPrefs() {
  try {
    const raw = localStorage.getItem(NOTIFY_PREFS_KEY);
    if (!raw) return { enabled: false };
    const o = JSON.parse(raw);
    return { enabled: !!o.enabled };
  } catch { return { enabled: false }; }
}
function saveNotifyPrefs(p) {
  try { localStorage.setItem(NOTIFY_PREFS_KEY, JSON.stringify(p)); } catch {}
}
let _notifyPrefs = loadNotifyPrefs();

function notificationsSupported() {
  return typeof window !== 'undefined' && 'Notification' in window;
}
function notificationsGranted() {
  return notificationsSupported() && Notification.permission === 'granted';
}
async function requestNotificationPermission() {
  if (!notificationsSupported()) return 'unsupported';
  if (Notification.permission === 'granted') return 'granted';
  if (Notification.permission === 'denied') return 'denied';
  try { return await Notification.requestPermission(); }
  catch { return Notification.permission || 'default'; }
}

// Snapshot of unread thread ids from the previous tick. On the next
// refresh, anything in new\old is brand-new unread → ping. `null`
// means "first pass, seed only" so loading the page doesn't blast the
// user with the entire backlog.
let _prevUnreadIds = null;

function _collectUnreadIndex() {
  const idx = new Map();
  for (const squad of (state.squads || [])) {
    const detail = state.squadDetails.get(squad.id);
    const threads = detail?.threads || [];
    for (const t of threads) {
      if (!isThreadUnread(t)) continue;
      idx.set(t.thread_id, { thread: t, squad });
    }
  }
  return idx;
}

function _fireThreadNotification(squad, thread) {
  if (!notificationsGranted() || !_notifyPrefs.enabled) return;
  if (typeof document !== 'undefined' && document.visibilityState === 'visible') return;
  if (thread.thread_id === state.currentThreadId) return;
  const squadLabel = (squad?.emoji ? squad.emoji + ' ' : '') + (squad?.name || squad?.id || '');
  const title = `OpenForge · ${squadLabel}`.trim();
  const body = (thread.title || thread.preview || '(new activity)').slice(0, 140);
  try {
    const n = new Notification(title, {
      body,
      tag: 'openforge:' + thread.thread_id,
      renotify: false,
      silent: false,
      icon: '/branding/logo-forge-f-256.png',
    });
    n.onclick = () => {
      try { window.focus(); } catch {}
      try {
        if (squad?.id) state.currentSquadId = squad.id;
        if (typeof selectThread === 'function') selectThread(thread.thread_id);
      } catch {}
      try { n.close(); } catch {}
    };
  } catch (err) {
    console.warn('[notify] failed:', err);
  }
}

function maybeNotifyNewUnread() {
  const idx = _collectUnreadIndex();
  const currentIds = new Set(idx.keys());
  if (!notificationsGranted() || !_notifyPrefs.enabled) {
    // Keep snapshot fresh so toggling on later doesn't replay backlog.
    _prevUnreadIds = currentIds;
    return;
  }
  if (_prevUnreadIds === null) {
    _prevUnreadIds = currentIds;
    return;
  }
  for (const id of currentIds) {
    if (_prevUnreadIds.has(id)) continue;
    const entry = idx.get(id);
    if (entry) _fireThreadNotification(entry.squad, entry.thread);
  }
  _prevUnreadIds = currentIds;
}

// ─── squads ───────────────────────────────────────────────────────────
async function loadSquads() {
  setStatus('加载 squads...');
  try {
    const url = showArchivedSquads ? '/api/squads?include_archived=1' : '/api/squads';
    state.squads = await apiJson(url);
    await Promise.all(state.squads.map(async squad => {
      const detail = await apiJson(`/api/squads/${encodeURIComponent(squad.id)}`);
      state.squadDetails.set(squad.id, detail);
    }));
    // First-run seed: 首次访问时 lastSeen 为空，把所有现有 thread 的 last_post_at
    // 当作“已知态”写进去，避免第一眼满屏红 badge。dora 提的 #2。
    if (Object.keys(_lastSeen).length === 0) {
      for (const squad of state.squads) {
        const d = state.squadDetails.get(squad.id);
        for (const t of (d?.threads || [])) {
          const lp = _normTs(t.last_post_at);
          if (t.thread_id && lp) _lastSeen[t.thread_id] = lp;
        }
      }
      _persistLastSeen();
    }
    if (!state.currentSquadId && state.squads.length) {
      state.currentSquadId = state.squads[0].id;
    }
    renderSquadRail();
    renderThreadRail();
    updateUnreadTitle();
    setStatus(`已加载 ${state.squads.length} 个 squad`);
  } catch (err) {
    setStatus(`加载失败: ${err.message}`, false);
  }
}

const DEFAULT_SQUAD_ID = null;  // Plan C: every squad is deletable

function renderSquadRail() {
  els.squadList.innerHTML = '';
  if (!state.squads.length) {
    const li = document.createElement('li');
    li.className = 'empty-row';
    li.innerHTML = '还没有 squad。<br>点击下方 <b>+ New Squad</b> 创建。';
    els.squadList.appendChild(li);
    return;
  }
  state.squads.forEach(squad => {
    const detail = state.squadDetails.get(squad.id);
    const count = detail?.threads?.length || 0;
    const unread = squadUnreadCount(squad.id);
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'squad-item'
      + (squad.id === state.currentSquadId ? ' active' : '')
      + (squad.archived ? ' archived' : '')
      + (unread > 0 ? ' has-unread' : '');
    const countHtml = unread > 0
      ? `<span class="squad-unread-badge" title="${unread} unread thread${unread === 1 ? '' : 's'}">${unread}</span>`
      : `<span class="squad-count">${count}</span>`;
    btn.innerHTML = `
      <span class="squad-emoji">${escapeHtml(squad.emoji || '#')}</span>
      <span class="squad-name">${escapeHtml(squad.name || squad.id)}${squad.archived ? ' <span class="archived-tag">archived</span>' : ''}</span>
      ${countHtml}
    `;
    btn.onclick = () => selectSquad(squad.id);
    li.appendChild(btn);
    els.squadList.appendChild(li);
  });
}

async function deleteSquad(squad, threadCount) {
  const label = squad.name || squad.id;
  let msg = `确定删除 squad “${label}” 吗？`;
  if (threadCount > 0) {
    msg += `\n\n⚠️ 该 squad 下还有 ${threadCount} 个 thread，删除后 thread 将不再出现在侧边栏（events.jsonl 本身仍保留在磁盘）。`;
  }
  if (!confirm(msg)) return;
  try {
    const res = await fetch(`/api/squads/${encodeURIComponent(squad.id)}`, { method: 'DELETE' });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`${res.status} ${text || res.statusText}`);
    }
    state.squadDetails.delete(squad.id);
    if (state.currentSquadId === squad.id) {
      state.currentSquadId = null;
      state.currentThreadId = null;
      state.currentThread = null;
    }
    setStatus(`已删除 squad “${label}”`);
    closeModal();
    await loadSquads();
  } catch (err) {
    setStatus(`删除失败: ${err.message}`, false);
    alert(`删除 squad 失败：${err.message}`);
  }
}

async function selectSquad(squadId) {
  state.currentSquadId = squadId;
  state.currentThreadId = null;
  state.currentThread = null;
  renderSquadRail();
  renderThreadRail();
  renderDetail();
  await refreshThreadsForCurrentSquad();
}

async function refreshThreadsForCurrentSquad() {
  if (!state.currentSquadId) return;
  try {
    const detail = await apiJson(`/api/squads/${encodeURIComponent(state.currentSquadId)}`);
    state.squadDetails.set(state.currentSquadId, detail);
    renderSquadRail();
    renderThreadRail();
    updateUnreadTitle();
  } catch (err) {
    setStatus(`thread 列表加载失败: ${err.message}`, false);
  }
}

// Refresh *all* loaded squads' detail so the unread badge on every squad
// in the rail (not just the currently-selected one) reflects new posts.
// Without this, badges only update when the user clicks into a squad —
// scott repro 2026-05-28: a thread in another squad got a new post, the
// squad's red dot didn't appear until he clicked it. Best-effort: per-
// squad fetch errors are swallowed, the next poll will retry.
async function refreshAllSquadsForUnread() {
  if (!state.squads || !state.squads.length) return;
  const results = await Promise.allSettled(
    state.squads.map(s => apiJson(`/api/squads/${encodeURIComponent(s.id)}`))
  );
  let changed = false;
  results.forEach((r, i) => {
    if (r.status === 'fulfilled' && r.value) {
      state.squadDetails.set(state.squads[i].id, r.value);
      changed = true;
    }
  });
  if (!changed) return;
  renderSquadRail();
  // currently-open squad's middle rail already refreshed by
  // refreshThreadsForCurrentSquad on the same tick; re-render anyway
  // so dot/preview stays in sync if poll lapped that path.
  renderThreadRail();
  updateUnreadTitle();
}

// ─── thread rail (middle) ─────────────────────────────────────────────
function renderThreadRail() {
  const detail = state.squadDetails.get(state.currentSquadId);
  const squad = detail?.squad || state.squads.find(s => s.id === state.currentSquadId);
  if (!squad) {
    els.squadTitle.textContent = 'No squads';
    els.squadDescription.textContent = '';
    els.threadList.innerHTML = '';
    els.btnEditSquad.hidden = true;
    return;
  }
  els.squadTitle.textContent = `${squad.emoji || '#'} ${squad.name || squad.id}`;
  els.squadDescription.textContent = squad.description
    || `${squad.chair} chairs · ${squad.members.length} members`;
  els.btnEditSquad.hidden = false;
  // PR-A: header 📁 chip — only when project_dir is configured AND validated.
  if (els.squadProjectChip) {
    const pd = squad.project_dir;
    const valid = squad.project_dir_valid === true;
    if (pd && valid) {
      const basename = pd.split('/').filter(Boolean).pop() || pd;
      els.squadProjectChip.textContent = `📁 ${basename}`;
      els.squadProjectChip.title = pd;
      els.squadProjectChip.hidden = false;
    } else {
      els.squadProjectChip.hidden = true;
      els.squadProjectChip.textContent = '';
      els.squadProjectChip.title = '';
    }
  }
  renderThreadList(detail?.threads || []);
}

function renderSidebarPresence(activeAgents) {
  // v0.2 thread-list presence: 16px breathing avatars over the time stamp,
  // max 3 + "+N" overflow. Empty list => zero-height (don't render). Per
  // bugfix/designer 2026-06-03: sidebar layer only answers "who's running
  // right now" — no fail/timeout dwell, no unread-badge semantics. Long-tail
  // grey ring (v1.1) is driven by earliest started_at on the slot.
  if (!Array.isArray(activeAgents) || activeAgents.length === 0) return '';
  const STALE_MS = 5 * 60 * 1000;
  const now = Date.now();
  const cap = 3;
  const head = activeAgents.slice(0, cap);
  const extra = activeAgents.length - head.length;
  const slots = head.map(a => {
    const id = a && a.agent_id;
    if (!id) return '';
    const name = displayName(id) || id;
    const tsMs = typeof a.started_at === 'number'
      ? (a.started_at > 1e12 ? a.started_at : a.started_at * 1000)
      : (typeof a.started_at === 'string' ? Date.parse(a.started_at) : NaN);
    const stale = Number.isFinite(tsMs) && (now - tsMs) >= STALE_MS;
    const ringCls = stale ? 'ring ring--stale' : 'ring';
    const tip = stale
      ? `${name} \u6267\u884c\u4e2d\uff08\u5df2 ${Math.floor((now - tsMs) / 60000)} \u5206\u949f\uff09`
      : `${name} \u6b63\u5728\u6267\u884c\u2026`;
    const avCls = avatarClass(id);
    const avStyle = avatarStyle(id);
    const letter = (name || '?').slice(0, 1).toUpperCase();
    return `<span class="sb-presence-slot ${avCls}"${avStyle} role="status" aria-label="${escapeAttr(tip)}" title="${escapeAttr(tip)}">${escapeHtml(letter)}<span class="${ringCls}"></span></span>`;
  }).join('');
  const overflow = extra > 0
    ? `<span class="sb-presence-slot sb-presence-plus" title="\u53e6\u6709 ${extra} \u4e2a agent \u5728\u8dd1">+${extra}</span>`
    : '';
  return `<span class="sb-presence" aria-label="\u8be5 thread \u6709 ${activeAgents.length} \u4e2a agent \u5728\u6267\u884c">${slots}${overflow}</span>`;
}

function renderThreadList(threads) {
  els.threadList.innerHTML = '';
  if (!threads.length) {
    els.threadList.innerHTML =
      '<li class="empty-row">还没有 thread，在下面输入一条开始。</li>';
    return;
  }

  // Sort: open (in_progress=true) first, then closed; within each group,
  // most recent activity first. Closed threads sink to the bottom so the
  // active conversations stay front and center.
  const sorted = threads.slice().sort((a, b) => {
    const aOpen = a.in_progress ? 1 : 0;
    const bOpen = b.in_progress ? 1 : 0;
    if (aOpen !== bOpen) return bOpen - aOpen;
    const aTime = a.last_post_at || 0;
    const bTime = b.last_post_at || 0;
    return bTime - aTime;
  });

  const hasOpen = sorted.some(t => t.in_progress);
  const hasClosed = sorted.some(t => !t.in_progress);
  let closedLabelInserted = false;

  sorted.forEach(t => {
    // Insert a small group label between active and closed threads
    // (only when both groups exist).
    if (!t.in_progress && hasOpen && hasClosed && !closedLabelInserted) {
      const label = document.createElement('li');
      label.className = 'thread-group-label';
      label.textContent = '已关闭';
      els.threadList.appendChild(label);
      closedLabelInserted = true;
    }

    const li = document.createElement('li');
    const closedCls = t.in_progress ? '' : ' thread-item--closed';
    const unread = isThreadUnread(t);
    li.className = 'thread-item' + closedCls
      + (t.thread_id === state.currentThreadId ? ' active' : '')
      + (unread ? ' thread-item--unread' : '');
    // dora's v2 规则：dot 语义从“thread 活着”收敛成“有未读”。
    //   open   + 未读 → 绿色 pulse dot
    //   open   + 已读 → 无 dot
    //   closed + 未读 → 红色实心 dot
    //   closed + 已读 → 无 dot
    // open/closed 这个状态本身靠 🔒 chip / 列表分组 表达，不需要再拼一层 dot。
    const liveDot = (unread && t.in_progress) ? '<span class="live-dot"></span>' : '';
    const unreadDot = (unread && !t.in_progress) ? '<span class="unread-dot" title="有新消息"></span>' : '';
    const closedChip = t.in_progress
      ? ''
      : '<span class="thread-closed-chip" title="Closed">🔒</span>';
    li.innerHTML = `
      <button type="button">
        <div class="thread-line-1">
          ${liveDot}
          ${unreadDot}
          <span class="thread-preview">${escapeHtml(t.title || t.preview || '(empty)')}</span>
          ${closedChip}
        </div>
        <div class="thread-line-2">
          <span class="thread-by">${escapeHtml(t.created_by)}</span>
          <span class="dot-sep">·</span>
          <span>${t.post_count} ${t.post_count === 1 ? 'post' : 'posts'}</span>
          <span class="thread-meta-right">
            ${renderSidebarPresence(t.active_agents)}
            <span class="thread-time">${escapeHtml(formatRelative(t.last_post_at))}</span>
          </span>
        </div>
      </button>
    `;
    li.querySelector('button').onclick = () => selectThread(t.thread_id);
    els.threadList.appendChild(li);
  });
}

// ─── detail (right pane) ──────────────────────────────────────────────
async function selectThread(threadId) {
  state.currentThreadId = threadId;
  cancelReply();
  renderThreadRail();
  try {
    state.currentThread = await apiJson(`/api/threads/${encodeURIComponent(threadId)}`);
    // Mark this thread as seen up to its newest post — user is now
    // looking at it. Also bump squad rail so the badge clears.
    markThreadSeen(threadId, state.currentThread?.last_post_at || Date.now());
    renderDetail();
    renderThreadRail();
    renderSquadRail();
    updateUnreadTitle();
    setStatus(`已加载 ${threadId}`);
  } catch (err) {
    state.currentThread = null;
    renderDetail();
    setStatus(`thread 加载失败: ${err.message}`, false);
  }
  openThreadEventStream(threadId);
}

// ─── SSE: live push of thread events ─────────────────────────────────
function openThreadEventStream(threadId) {
  closeThreadEventStream();
  if (!threadId || typeof EventSource === 'undefined') return;
  try {
    const url = `/api/threads/${encodeURIComponent(threadId)}/events`;
    const es = new EventSource(url);
    state.threadEventSource = es;
    state.threadEventThreadId = threadId;
    es.onmessage = () => {
      // any new event → refetch projection (cheap; reads jsonl).
      if (state.currentThreadId === threadId) refreshCurrentThread();
      if (state.currentSquadId) refreshThreadsForCurrentSquad();
    };
    es.addEventListener('hello', () => { /* connected */ });
    es.onerror = () => {
      // EventSource auto-reconnects; if it permanently closes the
      // 8s poll fallback will keep things working.
    };
  } catch {
    /* ignore — poll fallback still runs */
  }
}

function closeThreadEventStream() {
  if (state.threadEventSource) {
    try { state.threadEventSource.close(); } catch {}
  }
  state.threadEventSource = null;
  state.threadEventThreadId = null;
}

async function refreshCurrentThread() {
  if (!state.currentThreadId) return;
  try {
    state.currentThread = await apiJson(`/api/threads/${encodeURIComponent(state.currentThreadId)}`);
    // user is actively viewing → keep last-seen current as new posts stream in
    markThreadSeen(state.currentThreadId, state.currentThread?.last_post_at || Date.now());
    renderDetail({ keepScroll: true });
    updateUnreadTitle();
  } catch (err) {
    /* ignore transient */
  }
}

function renderDetail({ keepScroll = false } = {}) {
  const t = state.currentThread;
  if (!t) {
    els.detailTitle.textContent = state.currentSquadId
      ? '选择一个 thread'
      : '选择一个 squad';
    els.detailSub.textContent = '';
    els.detailStatus.textContent = 'idle';
    els.detailStatus.className = 'status-chip';
    els.detailParticipants.innerHTML = '';
    if (els.pinnedArea) { els.pinnedArea.innerHTML = ''; els.pinnedArea.hidden = true; }
    els.btnCloseThread.disabled = true;
    els.postComposerInput.disabled = true;
    els.btnSendPost.disabled = true;
    els.postList.innerHTML = '<div class="empty">从中栏选择一个 thread，或在中栏底部输入开始一个新 thread。</div>';
    return;
  }
  els.detailTitle.textContent = t.title || t.preview || '(empty)';
  const startedRel = formatRelative(t.started_at);
  els.detailSub.textContent =
    `${t.created_by} started · ${startedRel} · ${t.post_count} posts`;
  els.detailStatus.textContent = t.in_progress ? 'open' : 'closed';
  els.detailStatus.className = 'status-chip ' + (t.in_progress ? 'chip-open' : 'chip-closed');
  renderParticipants(t.participants);
  renderPinnedArea(t);
  els.btnCloseThread.disabled = !t.in_progress;
  els.btnCloseThread.textContent = t.in_progress ? 'Close' : 'Closed';
  els.postComposerInput.disabled = !t.in_progress;
  els.btnSendPost.disabled = !t.in_progress;

  const prevScroll = els.postList.scrollTop;
  const wasNearBottom = els.postList.scrollHeight - prevScroll - els.postList.clientHeight < 80;
  renderPosts(t.posts);
  if (keepScroll && !wasNearBottom) {
    els.postList.scrollTop = prevScroll;
  } else {
    els.postList.scrollTop = els.postList.scrollHeight;
  }
}

function renderParticipants(members) {
  els.detailParticipants.innerHTML = '';
  (members || []).slice(0, 6).forEach(name => {
    const cls = `mini-avatar avatar-default ${avatarClass(name)}`;
    let el;
    if (isEmployee(name)) {
      // V1.1: prefer per-thread explicit session if a thread is open;
      // falls back to main when participants are rendered before a
      // thread is selected.
      el = document.createElement('a');
      el.href = webchatLinkFor(name, state.currentThreadId || null);
      el.target = '_blank';
      el.rel = 'noopener noreferrer';
      el.className = `${cls} avatar-link`;
      el.title = state.currentThreadId
        ? `点击查看 ${displayName(name)} 在本 thread 的 session`
        : `点击查看 ${displayName(name)} 的 main session`;
    } else {
      el = document.createElement('div');
      el.className = cls;
      el.title = displayName(name);
    }
    if (name.toLowerCase() === 'scott' && (state.settings.myAvatarColor || '').trim()) {
      el.style.background = state.settings.myAvatarColor.trim();
    }
    el.innerHTML = renderDefaultAvatarInner(name);
    els.detailParticipants.appendChild(el);
  });
}

// ─── v0.10 thread-pin: Pinned area + chip context menu ──────────────
const PIN_CAP = 5;
const _refExistsCache = new Map(); // ref_id -> bool

async function _resolveRefExistsBatch(ids) {
  const need = ids.filter(id => !_refExistsCache.has(id));
  if (!need.length) return;
  try {
    const r = await fetch('/api/refs/exists', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: need }),
    });
    if (!r.ok) return;
    const j = await r.json();
    const results = j.results || {};
    for (const id of need) {
      _refExistsCache.set(id, !!results[id]);
    }
  } catch { /* offline; leave cache untouched */ }
}

function _findRefLabelFromThread(refId) {
  // Scan posts for [[ref:id]] tokens we've already resolved into chips —
  // fall back to ref_id when unknown.
  const t = state.currentThread;
  if (!t) return refId;
  for (const p of t.posts || []) {
    const re = new RegExp(`\\[\\[ref:${refId}\\]\\]`);
    if (re.test(p.content || '')) {
      // No reliable per-chip label here without re-resolving; reuse refId.
      break;
    }
  }
  return refId;
}

async function renderPinnedArea(t) {
  const root = els.pinnedArea;
  if (!root) return;
  const pins = Array.isArray(t.pinned_refs) ? t.pinned_refs : [];
  if (!pins.length) {
    root.innerHTML = '';
    root.hidden = true;
    return;
  }
  root.hidden = false;
  root.innerHTML =
    `<div class="pinned-header"><span class="pinned-icon">📌</span>` +
    `<span class="pinned-title">Pinned (${pins.length}/${PIN_CAP})</span></div>` +
    `<div class="pinned-chips">` +
    pins.map(p => {
      const rid = p.ref_id;
      const label = escapeHtml(_findRefLabelFromThread(rid));
      const by = escapeHtml(p.pinned_by || 'scott');
      const when = p.pinned_at ? escapeHtml(formatRelative(p.pinned_at)) : '';
      const stale = _refExistsCache.get(rid) === false;
      return `<div class="pinned-chip${stale ? ' is-stale' : ''}" data-ref-id="${escapeAttr(rid)}" tabindex="0" role="button" title="${stale ? '文件已失效，点击移除' : '打开 ' + escapeAttr(rid)}">` +
        `<span class="pinned-chip-icon">📄</span>` +
        `<span class="pinned-chip-label">${label}</span>` +
        `<span class="pinned-chip-meta">pinned by ${by}${when ? ' · ' + when : ''}</span>` +
        `<button type="button" class="pinned-chip-close" aria-label="unpin" data-unpin="1" data-ref-id="${escapeAttr(rid)}">×</button>` +
        `</div>`;
    }).join('') +
    `</div>`;
  // resolve existence lazily; re-render only on transition
  const ids = pins.map(p => p.ref_id);
  await _resolveRefExistsBatch(ids);
  // re-mark stale state without full re-render to avoid focus loss
  root.querySelectorAll('.pinned-chip').forEach(node => {
    const rid = node.getAttribute('data-ref-id');
    const stale = _refExistsCache.get(rid) === false;
    node.classList.toggle('is-stale', stale);
  });
}

async function _pinRef(refId) {
  const tid = state.currentThreadId;
  if (!tid || !refId) return;
  try {
    const r = await fetch(`/api/threads/${encodeURIComponent(tid)}/pinned-refs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref_id: refId, actor: 'scott' }),
    });
    if (r.status === 409) {
      const j = await r.json().catch(() => ({}));
      if (j.error === 'PIN_CAP_REACHED') toast(j.message || '请先 unpin 一个');
      else toast('已经 pin 过了');
      return;
    }
    if (!r.ok) { toast('pin 失败'); return; }
    refreshCurrentThread();
  } catch { toast('pin 失败'); }
}

async function _unpinRef(refId) {
  const tid = state.currentThreadId;
  if (!tid || !refId) return;
  try {
    const r = await fetch(
      `/api/threads/${encodeURIComponent(tid)}/pinned-refs/${encodeURIComponent(refId)}?actor=scott`,
      { method: 'DELETE' }
    );
    if (!r.ok) { toast('unpin 失败'); return; }
    _refExistsCache.delete(refId);
    refreshCurrentThread();
  } catch { toast('unpin 失败'); }
}

function _isRefPinned(refId) {
  const t = state.currentThread;
  if (!t || !Array.isArray(t.pinned_refs)) return false;
  return t.pinned_refs.some(p => p.ref_id === refId);
}

// Local toast helper (some modules define their own scoped one; this one
// lives at module-top so pin/unpin failures can surface anywhere).
function toast(msg) {
  let el = document.getElementById('toast');
  if (!el) return; // no toast slot in DOM
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 2200);
}

// Context menu (right-click on file chips + pinned chips)
let _ctxMenu = null;
function _closeCtxMenu() { if (_ctxMenu) { _ctxMenu.remove(); _ctxMenu = null; } }

function _openCtxMenu(x, y, items) {
  _closeCtxMenu();
  const menu = document.createElement('div');
  menu.className = 'chip-ctx-menu';
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  items.forEach(it => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip-ctx-item';
    btn.textContent = it.label;
    btn.addEventListener('click', () => { _closeCtxMenu(); it.onClick(); });
    menu.appendChild(btn);
  });
  document.body.appendChild(menu);
  _ctxMenu = menu;
}

document.addEventListener('click', _closeCtxMenu);
document.addEventListener('scroll', _closeCtxMenu, true);

document.addEventListener('contextmenu', e => {
  // Find a file-chip-ref OR pinned-chip target.
  const refChip = e.target.closest('.file-chip-ref');
  const pinChip = e.target.closest('.pinned-chip');
  const node = refChip || pinChip;
  if (!node) return;
  const rid = node.getAttribute('data-ref-id');
  if (!rid) return;
  // Only in a thread context.
  if (!state.currentThreadId) return;
  e.preventDefault();
  const items = [];
  if (_isRefPinned(rid)) {
    items.push({ label: '📌 Unpin (P)', onClick: () => _unpinRef(rid) });
  } else {
    items.push({ label: '📌 Pin to top (P)', onClick: () => _pinRef(rid) });
  }
  _openCtxMenu(e.clientX, e.clientY, items);
});

// × button on pinned chip
document.addEventListener('click', e => {
  const btn = e.target.closest('.pinned-chip-close[data-unpin="1"]');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  const rid = btn.getAttribute('data-ref-id');
  if (rid) _unpinRef(rid);
});

// Keyboard P toggle on focused chip (element-level, IME-safe).
let _imeComposingPin = false;
document.addEventListener('compositionstart', () => { _imeComposingPin = true; });
document.addEventListener('compositionend',   () => { _imeComposingPin = false; });
document.addEventListener('keydown', e => {
  if (_imeComposingPin) return;
  if (e.key !== 'p' && e.key !== 'P') return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const active = document.activeElement;
  if (!active) return;
  // Don't hijack when focus is in any editable text field.
  const tag = (active.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || active.isContentEditable) return;
  const chip = active.closest && (active.closest('.file-chip-ref') || active.closest('.pinned-chip'));
  if (!chip) return;
  const rid = chip.getAttribute('data-ref-id');
  if (!rid) return;
  e.preventDefault();
  if (_isRefPinned(rid)) _unpinRef(rid); else _pinRef(rid);
});


// Built fresh each render pass; renderPostNode reads it to look up
// parent posts for the inline quote card. Avoids walking state.currentThread
// inside the render loop.
let _postLookup = new Map();
// Chronological index of *live* posts only (drops superseded + drops
// __router__ placeholders/errors). Used to suppress the inline quote card
// when an agent reply's parent is the immediately-preceding human post —
// in that case the quoted text is right above and the card is pure noise
// (Scott 2026-05-24: "Sherry 回复中的 消息引用 我觉得没必要").
let _liveIndex = new Map();

function renderPosts(posts) {
  els.postList.innerHTML = '';
  const live = (posts || []).filter(p => !p.superseded);
  _postLookup = new Map();
  (posts || []).forEach(p => {
    const pid = p.id || p.post_id;
    if (pid) _postLookup.set(pid, p);
  });
  _liveIndex = new Map();
  live.forEach((p, i) => {
    const pid = p.id || p.post_id;
    if (pid) _liveIndex.set(pid, i);
  });
  if (!live.length) {
    els.postList.innerHTML = '<div class="empty">这条 thread 还没有 post。</div>';
    return;
  }
  // Build chip → reply pairing (PR 20:18). A regular post with
  // from_chip_post_id pointing at a phase=done chip means the agent's
  // real reply has landed; render duration inline on the reply header
  // and suppress the chip. Only phase=done suppresses — failed/skipped
  // chips are the only evidence and must stay visible.
  const _suppressedChipIds = new Set();
  const chipById = new Map();
  live.forEach(p => {
    if (p.post_type === 'status_chip') {
      const pid = p.id || p.post_id;
      if (pid) chipById.set(pid, p);
    }
  });
  live.forEach(p => {
    if (p.post_type === 'status_chip') return;
    const chipId = p.from_chip_post_id;
    if (!chipId) return;
    const chip = chipById.get(chipId);
    if (chip && chip.phase === 'done') {
      _suppressedChipIds.add(chipId);
      // Stash duration on the reply post object for renderPostNode header.
      if (chip.duration_ms != null) p._inlineDurationMs = chip.duration_ms;
    }
  });
  // V1.1: flat chronological list. Reply context is rendered as an inline
  // quote card at the top of each child post (see renderPostNode), not as
  // tree nesting — that was too visually heavy in real threads.
  live.forEach(p => {
    if (p.post_type === 'status_chip') {
      const pid = p.id || p.post_id;
      if (_suppressedChipIds.has(pid)) return; // duration moved to reply header
      els.postList.appendChild(renderAgentStatusChip(p));
    } else {
      els.postList.appendChild(renderPostNode(p, false));
    }
  });
  // Mermaid: turn ```mermaid fenced blocks (rendered by marked as
  // <pre><code class="language-mermaid">…</code></pre>) into live SVG.
  renderMermaidIn(els.postList);
}

// ─── mermaid renderer ──────────────────────────────────────────────
// Idempotent: each block gets a stable id and we skip already-rendered
// nodes (marked with data-mermaid-rendered="1"). Safe to re-run after
// every SSE-driven renderPosts pass.
let _mermaidSeq = 0;
function renderMermaidIn(root) {
  if (!root) return;
  const blocks = root.querySelectorAll('pre > code.language-mermaid, pre > code.lang-mermaid');
  if (!blocks.length) return;
  const run = () => {
    if (typeof window.mermaid === 'undefined' || !window.mermaid.render) return;
    blocks.forEach(code => {
      const pre = code.parentElement;
      if (!pre || pre.dataset.mermaidRendered === '1') return;
      const src = code.textContent || '';
      if (!src.trim()) return;
      const id = 'mmd-' + (Date.now().toString(36)) + '-' + (++_mermaidSeq);
      const host = document.createElement('div');
      host.className = 'mermaid-block';
      host.setAttribute('role', 'img');
      pre.dataset.mermaidRendered = '1';
      // Replace the <pre> with the host; keep a hidden source copy so
      // copy-paste / fallback still works if render throws.
      pre.replaceWith(host);
      try {
        window.mermaid.render(id, src).then(({ svg, bindFunctions }) => {
          host.innerHTML = svg;
          if (bindFunctions) bindFunctions(host);
        }).catch(err => {
          host.innerHTML = '<pre class="mermaid-error"><code></code></pre>';
          const codeEl = host.querySelector('code');
          if (codeEl) codeEl.textContent = '[mermaid] ' + (err && err.message || err) + '\n\n' + src;
        });
      } catch (err) {
        host.innerHTML = '<pre class="mermaid-error"><code></code></pre>';
        const codeEl = host.querySelector('code');
        if (codeEl) codeEl.textContent = '[mermaid] ' + (err && err.message || err) + '\n\n' + src;
      }
    });
  };
  if (typeof window.mermaid === 'undefined') {
    window.addEventListener('mermaid-ready', run, { once: true });
  } else {
    run();
  }
}

// ─── agent status chip (router placeholder replacement) ─────────────────
// post_type === 'status_chip' ；同一 dispatch 全程备同一个 post_id，后端
// 通过 patch_post / post_updated event 改 phase，前端 SSE 通道拿到后
// 重调 renderPosts 原地重渲染。
const _chipCollapseTimers = new Map();   // post_id → timer
const _chipCollapsed = new Set();        // post_ids 在 done -> done-collapsed
function renderAgentStatusChip(post) {
  const pid = post.id || post.post_id || '';
  // status_chip posts are authored by __router__ but represent a specific
  // agent's lifecycle; prefer the explicit agent_id field when present so
  // chips render with the agent's avatar/name, not router's.
  // Legacy fallback: pre-#22 chips lack agent_id; their content is the
  // string '<agent_id> thinking' (see _chip_content in post_router.py),
  // so we can recover the agent_id from the first token.
  let agent = post.agent_id;
  if (!agent) {
    const content = (post.content || '').trim();
    const m = content.match(/^([a-z][a-z0-9_-]*)\s+thinking$/i);
    if (m && m[1] !== '__router__') agent = m[1];
  }
  if (!agent) agent = post.speaker || '?';
  const name = displayName(agent) || agent;
  // 默认 phase=thinking（应对后端老据或 phase 丢失）
  let phase = post.phase || 'thinking';
  if (phase === 'done' && _chipCollapsed.has(pid)) phase = 'done-collapsed';

  const chip = document.createElement('div');
  chip.className = 'agent-status-chip';
  chip.dataset.postId = pid;
  chip.dataset.speaker = agent;
  chip.dataset.phase = phase;

  const avClass = avatarClass(agent);
  const avStyleAttr = avatarStyle(agent); // 可能为 '' 或 ' style="..."'
  const avLetter = (name || '?').slice(0, 1).toUpperCase();
  const avatar = `<span class="asc-avatar ${avClass}"${avStyleAttr}>${escapeHtml(avLetter)}</span>`;
  const nameHtml = `<span class="asc-name">${escapeHtml(name)}</span>`;
  const sep = `<span class="asc-sep">·</span>`;

  if (phase === 'thinking') {
    chip.innerHTML = `${avatar}${nameHtml}${sep}<span class="asc-phase">思考中…</span><span class="asc-spinner"></span>`;
  } else if (phase === 'running') {
    const tool = post.tool_name ? ` · ${escapeHtml(post.tool_name)}` : '';
    chip.innerHTML = `${avatar}${nameHtml}${sep}<span class="asc-phase">执行中${tool}</span><span class="asc-dot"></span>`;
  } else if (phase === 'done') {
    const dur = post.duration_ms != null
      ? ` · ${(post.duration_ms / 1000).toFixed(1)}s` : '';
    chip.innerHTML = `${avatar}${nameHtml}${sep}<span class="asc-phase">完成${dur}</span><span class="asc-icon">✓</span>`;
    // 2s 后自折叠：记下 pid，下次 renderPosts 重画时走 collapsed 分支
    if (!_chipCollapseTimers.has(pid) && !_chipCollapsed.has(pid)) {
      const t = setTimeout(() => {
        _chipCollapsed.add(pid);
        _chipCollapseTimers.delete(pid);
        // refetch + rerender。使用现有的刷新路径保持滚动位置。
        try { refreshCurrentThread(); } catch (_) {}
      }, 2000);
      _chipCollapseTimers.set(pid, t);
    }
  } else if (phase === 'done-collapsed') {
    const dur = post.duration_ms != null
      ? ` · ${(post.duration_ms / 1000).toFixed(1)}s` : '';
    chip.innerHTML = `<span class="asc-avatar ${avClass}"${avStyleAttr}>${escapeHtml(avLetter)}</span><span class="asc-name">${escapeHtml(name)} 完成${dur}</span>`;
  } else if (phase === 'failed') {
    const tipHtml = post.error
      ? `<span class="asc-tip">${escapeHtml(post.error)}</span>`
      : '';
    chip.innerHTML = `${avatar}${nameHtml}${sep}<span class="asc-phase">失败</span><span class="asc-icon">✕</span>` +
      `<span class="asc-actions">` +
      `<button type="button" class="asc-btn asc-retry">重试</button>` +
      `<button type="button" class="asc-btn asc-skip">跳过</button>` +
      `</span>${tipHtml}`;
    chip.querySelector('.asc-retry').onclick = (e) => {
      e.stopPropagation();
      _chipRetry(pid);
    };
    chip.querySelector('.asc-skip').onclick = (e) => {
      e.stopPropagation();
      _chipSkip(pid);
    };
  } else if (phase === 'skipped') {
    chip.innerHTML = `<span class="asc-avatar ${avClass}"${avStyleAttr}>${escapeHtml(avLetter)}</span><span class="asc-name">${escapeHtml(name)} 已跳过</span>`;
  } else {
    // unknown phase → fall back 到思考中外观，不报错
    chip.innerHTML = `${avatar}${nameHtml}${sep}<span class="asc-phase">${escapeHtml(phase)}</span>`;
  }
  return chip;
}

async function _chipRetry(pid) {
  if (!state.currentThreadId || !pid) return;
  try {
    await apiJson(`/api/threads/${encodeURIComponent(state.currentThreadId)}/posts/${encodeURIComponent(pid)}/retry`, {
      method: 'POST', body: '{}',
    });
    // SSE 会 refresh；主动拉一下以防 SSE 有延迟
    refreshCurrentThread();
  } catch (err) {
    setStatus(`重试失败: ${err.message}`, false);
  }
}
async function _chipSkip(pid) {
  if (!state.currentThreadId || !pid) return;
  try {
    await apiJson(`/api/threads/${encodeURIComponent(state.currentThreadId)}/posts/${encodeURIComponent(pid)}/skip`, {
      method: 'POST', body: '{}',
    });
    refreshCurrentThread();
  } catch (err) {
    setStatus(`跳过失败: ${err.message}`, false);
  }
}

// One-line preview of a post's content for use inside quote cards / banners.
// Strips markdown noise just enough to look clean in 1 line; never returns
// more than n chars.
function quotePreview(text, n = 140) {
  const raw = (text || '').replace(/\s+/g, ' ').trim();
  if (!raw) return '';
  return raw.length <= n ? raw : raw.slice(0, n - 1) + '…';
}

// Render the gray bordered "quoted message" card. Used in two places:
//   1. top of any post that has parent_post_id (inline quote)
//   2. composer reply banner (with the × cancel button)
// `opts.cancelable=true` adds the × button. Returns an HTMLElement.
function renderQuoteCard(parent, opts = {}) {
  const card = document.createElement('div');
  card.className = 'quote-card';
  const author = displayName(parent.speaker) || '?';
  // Prefer the ISO ts (locale-formatted) for the quote header — matches
  // the visual reference Scott showed ("2026/5/23 14:52").
  let timeLabel = parent.time || '';
  if (parent.ts) {
    try {
      const d = new Date(parent.ts);
      if (!isNaN(d.getTime())) {
        timeLabel = `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()} ` +
                    `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
      }
    } catch (e) { /* keep fallback */ }
  }
  const cancelBtn = opts.cancelable
    ? '<button type="button" class="quote-cancel" title="取消回复">×</button>'
    : '';
  card.innerHTML = `
    <div class="quote-head">
      <span class="quote-author">${escapeHtml(author)}</span>
      <span class="quote-time">${escapeHtml(timeLabel)}</span>
      ${cancelBtn}
    </div>
    <div class="quote-body">${escapeHtml(quotePreview(parent.content))}</div>
  `;
  if (!opts.cancelable) {
    // click-to-scroll only for inline quote cards rendered inside posts.
    card.classList.add('quote-clickable');
    card.title = '跳到原消息';
    card.onclick = (e) => {
      e.stopPropagation();
      const pid = parent.id || parent.post_id;
      if (pid) scrollToPost(pid);
    };
  }
  return card;
}

function scrollToPost(postId) {
  const target = document.querySelector(`.post[data-post-id="${CSS.escape(postId)}"]`);
  if (!target) return;
  target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  target.classList.remove('post-flash');
  // force reflow so the animation restarts even if user clicks twice in a row
  void target.offsetWidth;
  target.classList.add('post-flash');
  setTimeout(() => target.classList.remove('post-flash'), 1400);
}

function renderPostNode(post, _unused) {
  const row = document.createElement('article');
  row.className = 'post';
  row.dataset.speaker = post.speaker;
  const postId = post.id || post.post_id || '';
  row.dataset.postId = postId;
  // V1.1: Reply button is always available except on router system posts.
  // Old `state.settings.replyNesting` gating was removed when the inline
  // quote-card UI replaced the tree-nesting UI.
  const showReplyBtn = post.speaker !== '__router__';
  row.innerHTML = `
    ${renderAvatarTag(post.speaker, { styleAttr: avatarStyle(post.speaker), threadId: state.currentThreadId || null })}
    <div class="post-content">
      <div class="post-head">
        <span class="post-name" title="${escapeHtml(post.speaker)}">${escapeHtml(displayName(post.speaker))}</span>
        <span class="post-time" title="${escapeHtml(post.ts || '')}">${escapeHtml(post.time || '')}</span>${post._inlineDurationMs != null ? `<span class="post-duration" title="agent dispatch duration">· ${(post._inlineDurationMs / 1000).toFixed(1)}s</span>` : ''}
      </div>
      <div class="post-quote-slot"></div>
      <div class="post-body">${renderBody(post.content)}</div>
      <div class="post-reactions"></div>
    </div>
    <div class="post-actions">
      <button class="btn-react" type="button" title="添加表情回应">😊</button>
      ${showReplyBtn ? '<button class="btn-reply" type="button" title="回复这条">↩ Reply</button>' : ''}
    </div>
  `;
  // Inline quote card: skip if this post's parent is the immediately
  // preceding live (non-router) post — quoting context that's literally
  // one row above is just visual noise. Auto-injected agent replies
  // (router sets parent_post_id to the @-trigger) are the dominant case.
  // The card *does* render when the parent is further back, which is
  // exactly when a quote helps: scott manually replied to an old post,
  // or an agent replied across other intervening posts.
  const parentId = post.parent_post_id;
  if (parentId) {
    const parent = _postLookup.get(parentId);
    if (parent) {
      const myIdx = _liveIndex.get(postId);
      const parentIdx = _liveIndex.get(parentId);
      const adjacent = myIdx !== undefined && parentIdx !== undefined
        && myIdx - parentIdx === 1;
      if (!adjacent) {
        row.querySelector('.post-quote-slot').appendChild(renderQuoteCard(parent));
      }
    }
  }
  if (showReplyBtn) {
    row.querySelector('.btn-reply').onclick = (e) => {
      e.stopPropagation();
      startReplyTo(post);
    };
  }
  row.querySelector('.btn-react').onclick = (e) => {
    e.stopPropagation();
    openReactionPicker(e.currentTarget, postId);
  };
  renderReactionChips(row.querySelector('.post-reactions'), postId, post.reactions || {});
  return row;
}

const REACTION_QUICK_PICKS = ['👍', '🎉', '🚀', '❤️', '👀', '🙏'];
const REACTION_SELF = 'scott';

function renderReactionChips(container, postId, reactions) {
  container.innerHTML = '';
  const entries = Object.entries(reactions || {});
  if (!entries.length) {
    container.classList.remove('has-reactions');
    return;
  }
  container.classList.add('has-reactions');
  entries.forEach(([emoji, actors]) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'reaction-chip';
    if ((actors || []).includes(REACTION_SELF)) chip.classList.add('reacted');
    chip.title = (actors || []).join(', ');
    chip.innerHTML = `<span class="r-emoji">${escapeHtml(emoji)}</span><span class="r-count">${(actors || []).length}</span>`;
    chip.onclick = (e) => {
      e.stopPropagation();
      toggleReaction(postId, emoji);
    };
    container.appendChild(chip);
  });
}

let _reactionPickerEl = null;
function closeReactionPicker() {
  if (_reactionPickerEl) {
    _reactionPickerEl.remove();
    _reactionPickerEl = null;
    document.removeEventListener('click', _closeReactionPickerOnDocClick, true);
  }
}
function _closeReactionPickerOnDocClick(e) {
  if (_reactionPickerEl && !_reactionPickerEl.contains(e.target)) closeReactionPicker();
}
function openReactionPicker(anchorBtn, postId) {
  closeReactionPicker();
  const picker = document.createElement('div');
  picker.className = 'reaction-picker';
  REACTION_QUICK_PICKS.forEach(emoji => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'r-pick';
    b.textContent = emoji;
    b.onclick = (e) => {
      e.stopPropagation();
      closeReactionPicker();
      toggleReaction(postId, emoji);
    };
    picker.appendChild(b);
  });
  document.body.appendChild(picker);
  const r = anchorBtn.getBoundingClientRect();
  const pw = picker.offsetWidth;
  const ph = picker.offsetHeight;
  const vw = document.documentElement.clientWidth;
  let top = r.top - ph - 6;
  if (top < 8) top = r.bottom + 6;
  let left = r.left - 4;
  if (left + pw + 8 > vw) left = vw - pw - 8;
  if (left < 8) left = 8;
  picker.style.top = `${top}px`;
  picker.style.left = `${left}px`;
  _reactionPickerEl = picker;
  // defer attaching outside-click so the triggering click doesn't immediately close it
  setTimeout(() => document.addEventListener('click', _closeReactionPickerOnDocClick, true), 0);
}

async function toggleReaction(postId, emoji) {
  if (!state.currentThreadId || !postId || !emoji) return;
  try {
    await apiJson(`/api/threads/${encodeURIComponent(state.currentThreadId)}/posts/${encodeURIComponent(postId)}/reactions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ emoji, actor: REACTION_SELF }),
    });
    // SSE will refresh; do a tiny optimistic refetch in case SSE is offline
    refreshCurrentThread();
  } catch (err) {
    setStatus(`reaction failed: ${err.message}`, false);
  }
}

// renderPostsNested removed in V1.1 — the tree-nesting UI was replaced by
// inline quote cards (see renderQuoteCard + renderPostNode).

function startReplyTo(post) {
  state.replyTo = {
    post_id: post.id || post.post_id,
    speaker: post.speaker,
    content: post.content,
    ts: post.ts,
    time: post.time,
  };
  // Render the same gray quote card we use inline inside posts, with a ×
  // to cancel. Replacing the whole banner contents on each call — cheap and
  // avoids leaking handlers from a previous reply target.
  els.composerReplyBanner.innerHTML = '';
  const card = renderQuoteCard(post, { cancelable: true });
  els.composerReplyBanner.appendChild(card);
  els.composerReplyBanner.classList.add('active');
  const cancelBtn = card.querySelector('.quote-cancel');
  if (cancelBtn) cancelBtn.onclick = (e) => { e.stopPropagation(); cancelReply(); };
  els.postComposerInput.focus();
}

function cancelReply() {
  state.replyTo = null;
  els.composerReplyBanner.classList.remove('active');
  els.composerReplyBanner.innerHTML = '';
}

// ─── composer: new thread ─────────────────────────────────────────────
async function submitNewThread() {
  const content = buildSubmitContent(els.threadComposerInput);
  if (!content) return;
  if (!state.currentSquadId) {
    setStatus('请先选择一个 squad', false);
    return;
  }
  els.threadComposerInput.disabled = true;
  try {
    const thread = await apiJson(
      `/api/squads/${encodeURIComponent(state.currentSquadId)}/threads`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, created_by: 'scott' }),
      }
    );
    els.threadComposerInput.value = '';
    clearAttachments(els.threadComposerInput);
    updateComposerCount(els.threadComposerInput, els.threadComposerCount);
    autosize(els.threadComposerInput);
    await refreshThreadsForCurrentSquad();
    await selectThread(thread.thread_id);
  } catch (err) {
    setStatus(`新建 thread 失败: ${err.message}`, false);
  } finally {
    els.threadComposerInput.disabled = false;
    els.threadComposerInput.focus();
  }
}

// ─── composer: new post ───────────────────────────────────────────────
async function submitPost() {
  const content = buildSubmitContent(els.postComposerInput);
  if (!content || !state.currentThreadId) return;
  els.postComposerInput.disabled = true;
  els.btnSendPost.disabled = true;
  try {
    const updated = await apiJson(
      `/api/threads/${encodeURIComponent(state.currentThreadId)}/posts`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          content,
          speaker: 'scott',
          parent_post_id: state.replyTo?.post_id || null,
        }),
      }
    );
    els.postComposerInput.value = '';
    clearAttachments(els.postComposerInput);
    autosize(els.postComposerInput);
    cancelReply();
    state.currentThread = updated;
    renderDetail();
    refreshThreadsForCurrentSquad();  // update preview/last_post_at
    // v0.5+: if Activity view is showing, optimistically refresh its rows.
    try { window.__forgeActivityNudge && window.__forgeActivityNudge(); } catch (_) {}
  } catch (err) {
    setStatus(`发送失败: ${err.message}`, false);
  } finally {
    if (state.currentThread?.in_progress) {
      els.postComposerInput.disabled = false;
      els.btnSendPost.disabled = false;
      els.postComposerInput.focus();
    }
  }
}

async function closeCurrentThread() {
  if (!state.currentThreadId) return;
  if (!confirm('Close this thread? 关闭后无法继续发 post（除非将来支持 reopen）。')) return;
  try {
    state.currentThread = await apiJson(
      `/api/threads/${encodeURIComponent(state.currentThreadId)}/close`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ by: 'scott' }),
      }
    );
    renderDetail();
    refreshThreadsForCurrentSquad();
  } catch (err) {
    setStatus(`关闭失败: ${err.message}`, false);
  }
}

// ─── squad modal ──────────────────────────────────────────────────────
let modalMode = 'create';
let editingSquadId = null;

async function openModal() {
  modalMode = 'create';
  editingSquadId = null;
  els.form.reset();
  if (els.modalTitle) els.modalTitle.textContent = 'New Squad';
  if (els.btnSubmitSquad) els.btnSubmitSquad.textContent = 'Create';
  if (els.form.elements.id) els.form.elements.id.disabled = false;
  els.btnDeleteSquad.hidden = true;
  const archiveRow = document.getElementById('squad-archive-row');
  if (archiveRow) archiveRow.hidden = true;
  // PR-A: reset project_dir field for fresh squad creation.
  if (els.form.elements.project_dir) {
    els.form.elements.project_dir.value = '';
  }
  projectDirSetState('idle', null);
  await buildMemberControls();
  [...els.memberCheckboxes.querySelectorAll('input')].forEach((input, idx) => {
    input.checked = idx === 0;
  });
  syncChairOptions();
  els.modal.classList.add('open');
  els.modal.setAttribute('aria-hidden', 'false');
  els.form.elements.id.focus();
}

async function openEditModal(squad) {
  modalMode = 'edit';
  editingSquadId = squad.id;
  els.form.reset();
  if (els.modalTitle) els.modalTitle.textContent = `编辑 squad · ${squad.id}`;
  if (els.btnSubmitSquad) els.btnSubmitSquad.textContent = '保存';
  if (els.form.elements.id) {
    els.form.elements.id.value = squad.id;
    els.form.elements.id.disabled = true;
  }
  if (els.form.elements.name) els.form.elements.name.value = squad.name || '';
  if (els.form.elements.description) els.form.elements.description.value = squad.description || '';
  if (els.form.elements.project_dir) {
    els.form.elements.project_dir.value = squad.project_dir || '';
    projectDirSetState(els.form.elements.project_dir.value ? (squad.project_dir_valid === true ? 'valid' : 'idle') : 'idle', squad.project_dir_valid === true ? squad.project_dir : null);
  }
  if (els.form.elements.emoji) els.form.elements.emoji.value = squad.emoji || '';
  await buildMemberControls();
  // Make sure existing members are present as checkboxes even if /api/employees
  // didn't surface them (e.g. transient fetch failure or member is a legacy
  // entry without a workspace-<id>/SOUL.md).
  const members = new Set(squad.members || []);
  const known = new Set(
    [...els.memberCheckboxes.querySelectorAll('input')].map(i => i.value)
  );
  members.forEach(m => {
    if (!known.has(m)) {
      const label = document.createElement('label');
      label.innerHTML = `<input type="checkbox" name="members" value="${m}" /> ${m}`;
      label.querySelector('input').onchange = syncChairOptions;
      els.memberCheckboxes.appendChild(label);
    }
  });
  [...els.memberCheckboxes.querySelectorAll('input')].forEach(input => {
    input.checked = members.has(input.value);
  });
  syncChairOptions();
  if (squad.chair) els.chairSelect.value = squad.chair;
  const archiveRow = document.getElementById('squad-archive-row');
  if (archiveRow) {
    archiveRow.hidden = false;
    document.getElementById('squad-archived-cb').checked = !!squad.archived;
  }
  els.btnDeleteSquad.hidden = false;
  els.modal.classList.add('open');
  els.modal.setAttribute('aria-hidden', 'false');
}

function closeModal() {
  els.modal.classList.remove('open');
  els.modal.setAttribute('aria-hidden', 'true');
}

function syncChairOptions() {
  const selected = [...els.memberCheckboxes.querySelectorAll('input:checked')].map(i => i.value);
  els.chairSelect.innerHTML = '';
  selected.forEach(agent => {
    const opt = document.createElement('option');
    opt.value = agent;
    opt.textContent = agent;
    els.chairSelect.appendChild(opt);
  });
}

// Build the squad-modal member checkboxes from the curated employee
// roster (GET /api/employees). The roster is authoritative — it returns
// curated workspace-<id>/SOUL.md owners PLUS an opt-in runtime allowlist
// (codex, claude-code) on the server side, so adding a new employee
// requires zero front-end code changes.
async function buildMemberControls() {
  let employees = null;
  try {
    const res = await fetch('/api/employees');
    if (res.ok) employees = await res.json();
  } catch (e) { /* network error — fall back below */ }
  if (!Array.isArray(employees) || employees.length === 0) {
    // Last-resort fallback: legacy hardcoded list. Used only if /api/employees
    // is unreachable AND returned nothing usable, so the picker is never empty.
    employees = AGENTS.slice();
  }
  const list = employees.filter(a => typeof a === 'string' && a).sort();
  els.memberCheckboxes.innerHTML = '';
  list.forEach(agent => {
    const label = document.createElement('label');
    label.innerHTML = `<input type="checkbox" name="members" value="${agent}" /> ${agent}`;
    label.querySelector('input').onchange = syncChairOptions;
    els.memberCheckboxes.appendChild(label);
  });
  syncChairOptions();
}

// ─── composer helpers ─────────────────────────────────────────────────
function updateComposerCount(input, counter) {
  if (counter) counter.textContent = input.value.length;
}

// ─── paste-image upload ───────────────────────────────────────────
// Listen for `paste` events on a composer textarea; if the clipboard
// contains an image, POST it to /api/uploads and insert a markdown
// `![paste](/api/uploads/<sha>.<ext>)` reference at the caret. The
// renderer (renderBody) turns that into an inline <img>.

async function uploadPastedImage(file) {
  const buf = await file.arrayBuffer();
  // base64 in chunks (avoid 'Maximum call stack' on big buffers)
  const bytes = new Uint8Array(buf);
  let bin = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  const b64 = btoa(bin);
  const res = await fetch('/api/uploads', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      content_base64: b64,
      content_type: file.type || 'image/png',
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data; // { url, filename, size, content_type, sha256 }
}

function insertAtCaret(input, snippet) {
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? input.value.length;
  const before = input.value.slice(0, start);
  const after = input.value.slice(end);
  // Pad with surrounding newlines so the image renders on its own line.
  const needLeadNl = before.length > 0 && !before.endsWith('\n');
  const needTrailNl = after.length > 0 && !after.startsWith('\n');
  const insert = (needLeadNl ? '\n' : '') + snippet + (needTrailNl ? '\n' : '');
  input.value = before + insert + after;
  const caret = (before + insert).length;
  input.selectionStart = input.selectionEnd = caret;
  autosize(input);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

// ─── composer attachments ──────────────────────────────────────────
// Pasted images are NOT inserted into the textarea as markdown anymore;
// instead they live on `input._attachments = [{url, alt}]` and are shown
// in the sibling preview strip (with a × close button). On submit we
// concatenate them onto the message body.
function getAttachments(input) {
  if (!input._attachments) input._attachments = [];
  return input._attachments;
}
function addAttachment(input, att) {
  getAttachments(input).push(att);
  if (input._refreshPreview) input._refreshPreview();
}
function removeAttachment(input, idx) {
  const arr = getAttachments(input);
  arr.splice(idx, 1);
  if (input._refreshPreview) input._refreshPreview();
}
function clearAttachments(input) {
  input._attachments = [];
  if (input._refreshPreview) input._refreshPreview();
}
function buildSubmitContent(input) {
  const text = input.value.trim();
  const atts = getAttachments(input);
  if (atts.length === 0) return text;
  const imgMd = atts.map(a => `![${a.alt || 'paste'}](${a.url})`).join('\n');
  return text ? `${text}\n\n${imgMd}` : imgMd;
}

function attachPasteUpload(input) {
  if (!input || input.dataset.pasteUploadBound === '1') return;
  input.dataset.pasteUploadBound = '1';
  input.addEventListener('paste', async event => {
    const items = Array.from(event.clipboardData?.items || []);
    const imageItems = items.filter(it => it.kind === 'file' && /^image\//.test(it.type));
    if (imageItems.length === 0) return;
    event.preventDefault();
    setStatus(`上传图片中 (${imageItems.length})...`);
    try {
      for (const item of imageItems) {
        const file = item.getAsFile();
        if (!file) continue;
        const meta = await uploadPastedImage(file);
        addAttachment(input, { url: meta.url, alt: 'paste' });
      }
      setStatus('图片已上传 ✅');
    } catch (err) {
      setStatus(`图片上传失败: ${err.message}`, false);
    }
  });
}

// ─── composer image preview ───────────────────────────────────────
// Scan the composer textarea for `/api/uploads/<file>` URLs (from
// `![paste](...)` markdown) and render thumbnails in a sibling preview
// strip so users see what they pasted before submitting.
const COMPOSER_IMG_RE = /!\[([^\]]*)\]\((\/api\/uploads\/[A-Za-z0-9._-]+)\)/g;

function attachComposerPreview(input) {
  if (!input || input.dataset.previewBound === '1') return;
  input.dataset.previewBound = '1';
  const strip = document.createElement('div');
  strip.className = 'composer-preview';
  strip.hidden = true;
  input.insertAdjacentElement('beforebegin', strip);
  const update = () => {
    const items = getAttachments(input);
    if (items.length === 0) {
      strip.hidden = true;
      strip.innerHTML = '';
      return;
    }
    strip.hidden = false;
    strip.innerHTML = items.map((it, i) =>
      `<span class="composer-preview-thumb" data-idx="${i}" title="${escapeAttr(it.url)}">`
      + `<img src="${escapeAttr(it.url)}" alt="${escapeAttr(it.alt || 'image')}" loading="lazy" />`
      + `<button type="button" class="composer-preview-remove" data-idx="${i}" title="移除">×</button>`
      + `</span>`
    ).join('');
  };
  strip.addEventListener('click', e => {
    const btn = e.target.closest('.composer-preview-remove');
    if (!btn) return;
    e.preventDefault();
    const idx = Number(btn.dataset.idx);
    if (Number.isInteger(idx)) removeAttachment(input, idx);
  });
  input.addEventListener('input', update);
  // expose so the post-submit reset can clear the strip too
  input._refreshPreview = update;
}

function wireComposer(input, submit, counter) {
  input.addEventListener('input', () => {
    autosize(input);
    updateComposerCount(input, counter);
    updateMentionPicker(input);
    if (input._refreshPreview) input._refreshPreview();
  });
  input.addEventListener('keydown', event => {
    if (mentionPickerKeydown(input, event)) return;  // picker consumed it
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });
  input.addEventListener('blur', () => setTimeout(() => closeMentionPicker(), 120));
}

// ─── @-mention picker ─────────────────────────────────────────────────────
let _agentList = [];
async function refreshAgentList() {
  try {
    const res = await fetch('/api/agents');
    if (res.ok) _agentList = await res.json();
  } catch (e) { /* keep stale */ }
}

const picker = (() => {
  let el = null;
  let activeInput = null;
  let token = null;          // { start, end, query }
  let items = [];
  let highlight = 0;
  function ensureEl() {
    if (el) return el;
    el = document.createElement('div');
    el.className = 'mention-picker';
    el.style.display = 'none';
    document.body.appendChild(el);
    return el;
  }
  function position(input) {
    const r = input.getBoundingClientRect();
    ensureEl();
    el.style.left = (r.left + 8) + 'px';
    el.style.top = (r.top - 6) + 'px';
    el.style.transform = 'translateY(-100%)';
    el.style.minWidth = Math.min(220, r.width - 16) + 'px';
  }
  function render() {
    ensureEl();
    el.innerHTML = '';
    items.forEach((name, idx) => {
      const row = document.createElement('div');
      row.className = 'mention-item' + (idx === highlight ? ' active' : '');
      row.textContent = '@' + name;
      row.addEventListener('mousedown', e => {
        e.preventDefault();
        choose(idx);
      });
      el.appendChild(row);
    });
    el.style.display = items.length ? 'block' : 'none';
  }
  function open(input, tok, candidates) {
    activeInput = input;
    token = tok;
    items = candidates;
    highlight = 0;
    position(input);
    render();
  }
  function close() {
    activeInput = null;
    token = null;
    items = [];
    if (el) el.style.display = 'none';
  }
  function isOpen() { return items.length > 0 && el && el.style.display !== 'none'; }
  function choose(idx) {
    if (!activeInput || !token || !items[idx]) return close();
    const v = activeInput.value;
    const before = v.slice(0, token.start);
    const after = v.slice(token.end);
    const insert = '@' + items[idx] + ' ';
    activeInput.value = before + insert + after;
    const caret = (before + insert).length;
    activeInput.setSelectionRange(caret, caret);
    activeInput.dispatchEvent(new Event('input', { bubbles: true }));
    close();
    activeInput.focus();
  }
  function move(delta) {
    if (!items.length) return;
    highlight = (highlight + delta + items.length) % items.length;
    render();
  }
  function selectActive() { choose(highlight); }
  return { open, close, isOpen, move, selectActive };
})();

function _detectMentionToken(input) {
  const caret = input.selectionStart;
  if (caret == null) return null;
  const v = input.value.slice(0, caret);
  // match the trailing `@word` (word may be empty); only when @ is at start
  // or after whitespace, to avoid emails.
  const m = v.match(/(^|\s)@([\w-]*)$/);
  if (!m) return null;
  const query = m[2];
  const start = caret - query.length - 1;  // includes the @
  return { start, end: caret, query };
}

function updateMentionPicker(input) {
  const tok = _detectMentionToken(input);
  if (!tok) return picker.close();
  const q = tok.query.toLowerCase();
  const candidates = _agentList
    .filter(a => !q || a.toLowerCase().includes(q))
    .slice(0, 8);
  if (!candidates.length) return picker.close();
  picker.open(input, tok, candidates);
}

function closeMentionPicker() { picker.close(); }

function mentionPickerKeydown(input, event) {
  if (!picker.isOpen()) return false;
  if (event.key === 'ArrowDown') { event.preventDefault(); picker.move(1); return true; }
  if (event.key === 'ArrowUp')   { event.preventDefault(); picker.move(-1); return true; }
  if (event.key === 'Enter' || event.key === 'Tab') {
    event.preventDefault(); picker.selectActive(); return true;
  }
  if (event.key === 'Escape')    { event.preventDefault(); picker.close(); return true; }
  return false;
}

// ─── wire-up ──────────────────────────────────────────────────────────
els.btnNewSquad.onclick = openModal;
els.btnCloseModal.onclick = closeModal;
els.btnCancelModal.onclick = closeModal;
els.modal.onclick = event => {
  if (event.target === els.modal) closeModal();
};

// PR-A: Project directory field — 6-state machine (S0..S6) per design §2.4.
// State lives on the wrap span via data-state (consumed by CSS). The helper
// text is replaced, not stacked. /api/fs/validate is called once per blur,
// 200ms-debounced on identical path; in-flight requests abort on next blur.
const PROJECT_DIR_HELPER_DEFAULT = '这个 squad 的 agent 接到开发任务时会在此目录下创建 worktree。留空 = 纯讨论型 squad。';
let _projectDirAbort = null;
let _projectDirLastPath = null;
let _projectDirLastResult = null;
let _projectDirLastValidatedAt = 0;

function projectDirGetEls() {
  const input = document.getElementById('project-dir-input');
  const wrap = input ? input.closest('.project-dir-input-wrap') : null;
  const suffix = document.getElementById('project-dir-suffix');
  const helper = document.getElementById('project-dir-helper');
  return { input, wrap, suffix, helper };
}

function projectDirSetState(state, info) {
  const { input, wrap, suffix, helper } = projectDirGetEls();
  if (!input || !wrap || !helper) return;
  wrap.dataset.state = state;
  let text = helper.dataset.default || PROJECT_DIR_HELPER_DEFAULT;
  helper.classList.remove('is-ok', 'is-bad', 'is-warn');
  let icon = '';
  if (state === 'validating') {
    icon = '↻'; // CSS spins it
    text = '校验中…';
  } else if (state === 'valid') {
    icon = '✓';
    const base = ((info || '').split('/').filter(Boolean).pop()) || info || '';
    text = `已连接：${base}`;
    helper.classList.add('is-ok');
  } else if (state === 'not_exist') {
    icon = '✕';
    text = '❌ 路径不存在：检查盘是否挂载或路径是否拼错';
    helper.classList.add('is-bad');
  } else if (state === 'not_git') {
    icon = '✕';
    text = '❌ 不是 git 仓库（缺少 .git 目录）：请确认这是 git 项目的根目录';
    helper.classList.add('is-bad');
  } else if (state === 'warn') {
    icon = '⚠';
    text = '⚠️ 暂时无法校验路径，提交时会重新检查';
    helper.classList.add('is-warn');
  }
  if (suffix) suffix.textContent = icon;
  helper.textContent = text;
}

async function projectDirValidate(rawPath) {
  const { input } = projectDirGetEls();
  if (!input) return;
  const path = (rawPath || '').trim();
  if (!path) {
    projectDirSetState('idle', null);
    _projectDirLastPath = null;
    _projectDirLastResult = null;
    return;
  }
  // 200ms debounce on identical path — don't re-fetch what we already have.
  const now = Date.now();
  if (path === _projectDirLastPath
      && _projectDirLastResult
      && (now - _projectDirLastValidatedAt) < 200) {
    const r = _projectDirLastResult;
    if (r.exists && r.is_git_repo) projectDirSetState('valid', path);
    else if (!r.exists) projectDirSetState('not_exist', null);
    else projectDirSetState('not_git', null);
    return;
  }
  if (_projectDirAbort) {
    try { _projectDirAbort.abort(); } catch (_) { /* noop */ }
  }
  _projectDirAbort = new AbortController();
  projectDirSetState('validating', null);
  try {
    const r = await fetch(`/api/fs/validate?path=${encodeURIComponent(path)}`, {
      signal: _projectDirAbort.signal,
    });
    let data = {};
    try { data = await r.json(); } catch (_) { /* empty body */ }
    if (r.status === 400) {
      // not absolute / malformed — surface as a path error.
      projectDirSetState('not_exist', null);
      return;
    }
    if (!r.ok) {
      projectDirSetState('warn', null);
      return;
    }
    _projectDirLastPath = path;
    _projectDirLastResult = data;
    _projectDirLastValidatedAt = Date.now();
    if (data.exists && data.is_git_repo) projectDirSetState('valid', path);
    else if (!data.exists) projectDirSetState('not_exist', null);
    else projectDirSetState('not_git', null);
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    projectDirSetState('warn', null);
  }
}

function projectDirBindBlur() {
  const { input } = projectDirGetEls();
  if (!input || input.dataset.pdBound) return;
  input.dataset.pdBound = '1';
  input.addEventListener('blur', () => projectDirValidate(input.value));
  input.addEventListener('input', () => {
    // Per design §2.4: while typing, return to idle (no per-keystroke fetch).
    if (!input.value.trim()) {
      projectDirSetState('idle', null);
      _projectDirLastPath = null;
      _projectDirLastResult = null;
      return;
    }
    const { wrap } = projectDirGetEls();
    if (wrap && wrap.dataset.state !== 'idle' && wrap.dataset.state !== 'focused') {
      projectDirSetState('idle', null);
    }
  });
  input.addEventListener('focus', () => {
    const { wrap } = projectDirGetEls();
    if (wrap && (wrap.dataset.state === 'idle' || !wrap.dataset.state)) {
      wrap.dataset.state = 'focused';
    }
  });
}
projectDirBindBlur();


els.form.onsubmit = async event => {
  event.preventDefault();
  const members = [...els.memberCheckboxes.querySelectorAll('input:checked')].map(i => i.value);
  if (!members.length) {
    setStatus('至少选择一个 member', false);
    return;
  }
  const payload = {
    name: els.form.elements.name.value.trim(),
    description: els.form.elements.description.value.trim(),
    emoji: els.form.elements.emoji.value.trim(),
    members,
    chair: els.chairSelect.value,
    project_dir: (els.form.elements.project_dir?.value || '').trim() || null,
  };
  const archivedCb = document.getElementById('squad-archived-cb');
  if (modalMode === 'edit' && archivedCb) {
    payload.archived = !!archivedCb.checked;
  }
  try {
    if (modalMode === 'edit' && editingSquadId) {
      await apiJson(`/api/squads/${encodeURIComponent(editingSquadId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      setStatus(`已更新 ${editingSquadId}`);
      const keepId = editingSquadId;
      closeModal();
      state.currentSquadId = keepId;
      await loadSquads();
    } else {
      payload.id = els.form.elements.id.value.trim();
      const squad = await apiJson('/api/squads', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      state.currentSquadId = squad.id;
      closeModal();
      await loadSquads();
    }
  } catch (err) {
    setStatus(`保存失败: ${err.message}`, false);
  }
};

els.btnEditSquad.onclick = () => {
  const detail = state.squadDetails.get(state.currentSquadId);
  const squad = detail?.squad || state.squads.find(s => s.id === state.currentSquadId);
  if (squad) openEditModal(squad);
};
els.btnDeleteSquad.onclick = () => {
  if (!editingSquadId) return;
  const squad = state.squads.find(s => s.id === editingSquadId);
  if (!squad) return;
  const detail = state.squadDetails.get(squad.id);
  deleteSquad(squad, detail?.threads?.length || 0);
};

wireComposer(els.threadComposerInput, submitNewThread, els.threadComposerCount);
wireComposer(els.postComposerInput, submitPost, null);
attachPasteUpload(els.postComposerInput);
attachPasteUpload(els.threadComposerInput);
attachComposerPreview(els.postComposerInput);
attachComposerPreview(els.threadComposerInput);
els.btnSendPost.onclick = submitPost;
els.btnCloseThread.onclick = closeCurrentThread;
els.btnRefreshThreads.onclick = refreshThreadsForCurrentSquad;
els.btnRefreshDetail.onclick = refreshCurrentThread;

const toggleArchivedBtn = document.getElementById('toggle-archived');
if (toggleArchivedBtn) {
  toggleArchivedBtn.onclick = async () => {
    showArchivedSquads = !showArchivedSquads;
    toggleArchivedBtn.textContent = (showArchivedSquads ? '☑' : '☐') + ' 归档';
    toggleArchivedBtn.classList.toggle('on', showArchivedSquads);
    await loadSquads();
  };
}

// ─── settings modal ────────────────────────────────────
function openSettingsModal() {
  for (const [key, val] of Object.entries(state.settings)) {
    const el = els.settingsForm.elements[key];
    if (!el) continue;
    if (el.type === 'checkbox') el.checked = !!val;
    else el.value = val ?? '';
  }
  refreshAvatarPreview();
  syncNotifyToggleUI();
  els.settingsModal.classList.add('open');
  els.settingsModal.setAttribute('aria-hidden', 'false');
}

function refreshAvatarPreview() {
  const prev = document.getElementById('avatar-preview');
  if (!prev) return;
  prev.textContent = avatarLabel('scott');
  const color = (state.settings.myAvatarColor || '').trim();
  prev.style.background = color || getDefaultScottBg();
  // sync color swatch dot
  const dot = document.querySelector('.color-swatch-dot');
  if (dot) dot.style.setProperty('--swatch', color || getDefaultScottBg());
  // sync emoji quick-pick active state
  const current = (state.settings.myAvatar || '').trim();
  document.querySelectorAll('.emoji-quick button').forEach(b => {
    b.classList.toggle('active', (b.dataset.emoji || '') === current || (!current && b.dataset.emoji === 'S'));
  });
}

function getDefaultScottBg() {
  // matches .av-default in style.css
  return '#616061';
}
function closeSettingsModal() {
  els.settingsModal.classList.remove('open');
  els.settingsModal.setAttribute('aria-hidden', 'true');
}
// ─── settings: OS notification toggle ──────────────────────────────
function syncNotifyToggleUI() {
  const cb = document.getElementById('notify-toggle');
  const status = document.getElementById('notify-status');
  if (!cb) return;
  if (!notificationsSupported()) {
    cb.checked = false;
    cb.disabled = true;
    if (status) status.textContent = '不支持';
    return;
  }
  cb.disabled = false;
  const perm = Notification.permission;
  cb.checked = !!_notifyPrefs.enabled && perm === 'granted';
  if (status) {
    if (perm === 'denied') status.textContent = '浏览器拒绝了权限';
    else if (perm === 'granted') status.textContent = cb.checked ? '已启用' : '已授权，未启用';
    else status.textContent = '未授权';
  }
}

async function handleNotifyToggleChange(ev) {
  const cb = ev.target;
  if (!cb) return;
  if (!cb.checked) {
    _notifyPrefs.enabled = false;
    saveNotifyPrefs(_notifyPrefs);
    syncNotifyToggleUI();
    return;
  }
  const result = await requestNotificationPermission();
  if (result === 'granted') {
    _notifyPrefs.enabled = true;
    saveNotifyPrefs(_notifyPrefs);
    // One-shot “you’re all set” ping so user sees what to expect.
    try {
      const t = new Notification('OpenForge', {
        body: '系统通知已启用',
        tag: 'openforge:test',
        silent: false,
        icon: '/branding/logo-forge-f-256.png',
      });
      setTimeout(() => { try { t.close(); } catch {} }, 4000);
    } catch {}
  } else {
    _notifyPrefs.enabled = false;
    saveNotifyPrefs(_notifyPrefs);
    cb.checked = false;
  }
  syncNotifyToggleUI();
}

document.addEventListener('DOMContentLoaded', () => {
  const cb = document.getElementById('notify-toggle');
  if (cb) cb.addEventListener('change', handleNotifyToggleChange);
  syncNotifyToggleUI();
});

els.btnSettings && (els.btnSettings.onclick = openSettingsModal);
els.btnCloseSettings && (els.btnCloseSettings.onclick = closeSettingsModal);
els.btnCloseSettings2 && (els.btnCloseSettings2.onclick = closeSettingsModal);
els.settingsModal && els.settingsModal.addEventListener('click', (e) => {
  if (e.target === els.settingsModal) closeSettingsModal();
});
els.settingsForm && els.settingsForm.addEventListener('change', (e) => {
  const t = e.target;
  if (!t || !t.name) return;
  if (t.type === 'checkbox') state.settings[t.name] = t.checked;
  else state.settings[t.name] = t.value;
  saveSettings(state.settings);
  refreshAvatarPreview();
  if (state.currentThread) renderDetail({ keepScroll: true });
  // (V1.1: reply is always on; no-op kept for call-site stability)
});
els.settingsForm && els.settingsForm.addEventListener('input', (e) => {
  const t = e.target;
  if (!t || !t.name || t.type === 'checkbox') return;
  state.settings[t.name] = t.value;
  saveSettings(state.settings);
  refreshAvatarPreview();
  if (state.currentThread) renderDetail({ keepScroll: true });
});
// emoji quick picks: set myAvatar text + sync
document.querySelectorAll('.emoji-quick button').forEach(btn => {
  btn.addEventListener('click', () => {
    const e = btn.dataset.emoji || '';
    const v = (e === 'S') ? '' : e;  // 'S' button == default (clear field)
    state.settings.myAvatar = v;
    const input = els.settingsForm.elements['myAvatar'];
    if (input) input.value = v;
    saveSettings(state.settings);
    refreshAvatarPreview();
    if (state.currentThread) renderDetail({ keepScroll: true });
  });
});
const btnResetAvatar = document.getElementById('btn-reset-avatar');
btnResetAvatar && btnResetAvatar.addEventListener('click', () => {
  state.settings.myAvatar = '';
  state.settings.myAvatarColor = '';
  const ta = els.settingsForm.elements['myAvatar'];
  const tc = els.settingsForm.elements['myAvatarColor'];
  if (ta) ta.value = '';
  if (tc) tc.value = '#616061';
  saveSettings(state.settings);
  refreshAvatarPreview();
  if (state.currentThread) renderDetail({ keepScroll: true });
});

// (btn-cancel-reply listener gone — see renderQuoteCard cancelable handler)

// poll for updates while a thread is open
function startPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(() => {
    if (state.currentThreadId) refreshCurrentThread();
    // Refresh every squad's detail — unread badges on un-selected squads
    // need to react to new posts too. This subsumes the per-current-squad
    // refresh (it's a strict superset).
    refreshAllSquadsForUnread();
  }, POLL_MS);
}

// ─── column resizing ───────────────────────────────────────
const GUTTER_KEYS = { squad: '--w-squad', thread: '--w-thread' };
const GUTTER_MIN  = { squad: 180,        thread: 220        };
const GUTTER_MAX  = { squad: 480,        thread: 600        };
const LS_KEY = 'openforge.colwidths.v1';

function loadColWidths() {
  try {
    const saved = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
    for (const [k, v] of Object.entries(saved)) {
      if (typeof v === 'number' && GUTTER_KEYS[k]) {
        document.documentElement.style.setProperty(GUTTER_KEYS[k], v + 'px');
      }
    }
  } catch { /* ignore */ }
}
function saveColWidth(name, px) {
  let cur = {};
  try { cur = JSON.parse(localStorage.getItem(LS_KEY) || '{}'); } catch {}
  cur[name] = px;
  localStorage.setItem(LS_KEY, JSON.stringify(cur));
}
function wireGutter(el) {
  const name = el.dataset.gutter;
  const cssVar = GUTTER_KEYS[name];
  if (!cssVar) return;
  el.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(cssVar)) ||
                   (name === 'squad' ? 260 : 320);
    el.classList.add('dragging');
    document.body.classList.add('col-resizing');
    const onMove = (ev) => {
      let next = startW + (ev.clientX - startX);
      next = Math.max(GUTTER_MIN[name], Math.min(GUTTER_MAX[name], next));
      document.documentElement.style.setProperty(cssVar, next + 'px');
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      el.classList.remove('dragging');
      document.body.classList.remove('col-resizing');
      const finalW = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(cssVar));
      if (Number.isFinite(finalW)) saveColWidth(name, Math.round(finalW));
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  });
  // double-click resets to default
  el.addEventListener('dblclick', () => {
    document.documentElement.style.removeProperty(cssVar);
    let cur = {};
    try { cur = JSON.parse(localStorage.getItem(LS_KEY) || '{}'); } catch {}
    delete cur[name];
    localStorage.setItem(LS_KEY, JSON.stringify(cur));
  });
}
loadColWidths();
document.querySelectorAll('.col-gutter').forEach(wireGutter);

buildMemberControls();
// PR-3 (PRD-v1.0 §4): fetch webchat base URL + employee roster at boot
// so renderPostNode / renderParticipants can render employee avatars as
// deep-links. Both calls degrade gracefully (default URL / empty set)
// so they never block initial rendering.
Promise.all([loadWebchatBase(), loadEmployeeSet()]).finally(() => {
  loadSquads().then(() => {
    refreshAgentList();
    startPolling();
    // Dev helper: ?devOpen=squad:thread auto-selects a thread for screenshot
    // automation. Cheap, side-effect-free when param absent; not exposed in UI.
    try {
      const m = new URLSearchParams(location.search).get('devOpen');
      if (m && m.includes(':')) {
        const [sid, tid] = m.split(':', 2);
        if (sid) selectSquad(sid).then(() => tid && selectThread(tid));
      }
    } catch (_) {}
  });
});

/* ─── v0.10: Thread create modal (➕ in thread list header + ⌘N) ─────── */
(function () {
  const TITLE_MAX = 80;
  const IMG_MAX = 9;
  const ALLOWED_MIME = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);

  const modal = document.getElementById('thread-create-modal');
  if (!modal) return; // modal markup missing — bail

  const els = {
    backdrop: modal,
    titleInput: document.getElementById('tc-title'),
    titleCount: document.getElementById('tc-title-count'),
    titleErr: document.getElementById('tc-title-err'),
    contentInput: document.getElementById('tc-content'),
    chipsBox: document.getElementById('tc-chips'),
    helperStatus: document.getElementById('tc-helper-status'),
    alert: document.getElementById('tc-alert'),
    form: document.getElementById('tc-form'),
    btnSubmit: document.getElementById('tc-btn-submit'),
    btnCancel: document.getElementById('tc-btn-cancel'),
    btnClose: document.getElementById('tc-btn-close'),
    btnNew: document.getElementById('btn-new-thread'),
    toast: document.getElementById('toast'),
  };

  // ── lightweight toast (independent of the IIFE-scoped one) ──
  function toast(msg) {
    const t = els.toast;
    if (!t) return;
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { t.hidden = true; }, 2000);
  }

  // ── state ──
  // chip: {id, name, status: 'uploading'|'ok'|'failed', url?, refId?, blob?, mime?, err?}
  let chips = [];
  let chipSeq = 0;
  let submitting = false;
  let lastFocusedEl = null;
  let titleTouched = false;

  function isOpen() { return modal.classList.contains('open'); }

  function hasAnyInput() {
    return (els.titleInput.value || '').trim() !== ''
      || (els.contentInput.value || '').trim() !== ''
      || chips.length > 0;
  }

  function anyUploading() { return chips.some(c => c.status === 'uploading'); }
  function anyFailed() { return chips.some(c => c.status === 'failed'); }

  function escapeHtmlLocal(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ── title validation ──
  function validateTitle() {
    const raw = els.titleInput.value || '';
    const trimmed = raw.trim();
    let err = '';
    if (trimmed.length === 0) {
      err = '请输入 title';
    } else if (trimmed.length > TITLE_MAX) {
      err = `title 最多 ${TITLE_MAX} 字`;
    }
    return { ok: !err, trimmed, err };
  }

  function refreshTitleUI() {
    const raw = els.titleInput.value || '';
    // Hard cap on raw length too — A-3 says "超 80 截断不允许输入".
    if (raw.length > TITLE_MAX) {
      els.titleInput.value = raw.slice(0, TITLE_MAX);
    }
    const v = els.titleInput.value;
    els.titleCount.textContent = `${v.length} / ${TITLE_MAX}`;
    const { ok, err } = validateTitle();
    if (titleTouched && !ok) {
      els.titleInput.classList.add('error');
      els.titleErr.textContent = `⚠ ${err}`;
    } else {
      els.titleInput.classList.remove('error');
      els.titleErr.textContent = '';
    }
    refreshSubmitState();
  }

  function refreshSubmitState() {
    const { ok } = validateTitle();
    const block = !ok || anyUploading() || submitting;
    els.btnSubmit.disabled = block;
    if (submitting) {
      els.helperStatus.textContent = '创建中…';
    } else if (anyUploading()) {
      els.helperStatus.textContent = '图片上传中…';
    } else if (anyFailed()) {
      els.helperStatus.textContent = '⚠ 有图片上传失败';
    } else {
      els.helperStatus.textContent = '';
    }
  }

  // ── auto-grow textarea ──
  function autosizeContent() {
    const ta = els.contentInput;
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 280) + 'px';
  }

  // ── chips ──
  function renderChips() {
    if (chips.length === 0) {
      els.chipsBox.hidden = true;
      els.chipsBox.innerHTML = '';
      return;
    }
    els.chipsBox.hidden = false;
    els.chipsBox.innerHTML = chips.map(c => {
      if (c.status === 'uploading') {
        return `<div class="tc-chip uploading" data-id="${c.id}">`
          + `<div class="tc-spinner"></div>`
          + `<span class="tc-chip-label">${escapeHtmlLocal(c.name)}</span>`
          + `</div>`;
      }
      if (c.status === 'failed') {
        return `<div class="tc-chip failed" data-id="${c.id}">`
          + `<span class="tc-chip-label">⚠ ${escapeHtmlLocal(c.err || '上传失败')}</span>`
          + `<div class="tc-chip-retry">`
          +   `<button type="button" class="tc-mini-btn" data-action="retry" data-id="${c.id}">重试</button>`
          +   `<button type="button" class="tc-mini-btn" data-action="remove" data-id="${c.id}">删除</button>`
          + `</div>`
          + `</div>`;
      }
      return `<div class="tc-chip" data-id="${c.id}">`
        + `<img src="${escapeHtmlLocal(c.url)}" alt="${escapeHtmlLocal(c.name)}" />`
        + `<button type="button" class="tc-chip-x" data-action="remove" data-id="${c.id}" title="移除" aria-label="移除图片">×</button>`
        + `</div>`;
    }).join('');
  }

  els.chipsBox.addEventListener('click', e => {
    if (submitting) return;
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const id = btn.dataset.id;
    const action = btn.dataset.action;
    const chip = chips.find(c => c.id === id);
    if (!chip) return;
    if (action === 'remove') {
      removeChip(id);
    } else if (action === 'retry') {
      retryChip(chip);
    }
  });

  function removeChip(id) {
    const chip = chips.find(c => c.id === id);
    if (!chip) return;
    // Remove [[ref:<id>]] token from content if present
    if (chip.refId) {
      const token = `[[ref:${chip.refId}]]`;
      els.contentInput.value = els.contentInput.value.split(token).join('').replace(/\n{3,}/g, '\n\n');
      autosizeContent();
    }
    chips = chips.filter(c => c.id !== id);
    renderChips();
    refreshSubmitState();
  }

  async function retryChip(chip) {
    if (!chip.blob) {
      chip.err = '图片数据已丢失';
      chip.status = 'failed';
      renderChips();
      refreshSubmitState();
      return;
    }
    chip.status = 'uploading';
    chip.err = '';
    renderChips();
    refreshSubmitState();
    try {
      const meta = await uploadAndRegister(chip.blob, chip.mime, chip.name);
      chip.status = 'ok';
      chip.url = meta.url;
      chip.refId = meta.refId;
      insertRefToken(chip.refId);
      renderChips();
      refreshSubmitState();
    } catch (err) {
      chip.status = 'failed';
      chip.err = (err && err.message) || '上传失败';
      renderChips();
      refreshSubmitState();
    }
  }

  function insertRefToken(refId) {
    // Insert at the current caret position; if textarea isn't focused,
    // append.
    const ta = els.contentInput;
    const token = `[[ref:${refId}]]`;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    if (document.activeElement === ta && start != null && end != null) {
      const before = ta.value.slice(0, start);
      const after = ta.value.slice(end);
      const needLead = before.length > 0 && !before.endsWith('\n') ? ' ' : '';
      const insert = needLead + token + ' ';
      ta.value = before + insert + after;
      const caret = (before + insert).length;
      ta.selectionStart = ta.selectionEnd = caret;
    } else {
      const pad = ta.value && !ta.value.endsWith('\n') ? '\n' : '';
      ta.value = ta.value + pad + token + ' ';
    }
    autosizeContent();
  }

  // ── upload pipeline: bytes → POST /api/uploads/refs (writes to operator workspace + registers ref) ──
  async function uploadAndRegister(blob, mime, label) {
    const buf = await blob.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = '';
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    const b64 = btoa(bin);
    const body = {
      content_base64: b64,
      content_type: mime,
      label: label || 'paste.png',
      source_agent: 'scott',
    };
    if (state.currentSquadId) body.squad_id = state.currentSquadId;
    const res = await fetch('/api/uploads/refs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-OpenForge-UI': '1' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    // data: { id, label, abs_path, ..., upload: { url?, filename, ... } }
    // /api/uploads/refs writes outside the openforge uploads dir, so there's
    // no /api/uploads/<filename> URL. We resolve content through the refs
    // pipeline: /api/refs/<id>/content serves the bytes.
    const refId = data.id;
    const url = `/api/refs/${encodeURIComponent(refId)}/content`;
    return { url, refId, filename: data.upload && data.upload.filename };
  }

  // ── paste handler ──
  els.contentInput.addEventListener('paste', async event => {
    if (submitting) return;
    const items = Array.from(event.clipboardData?.items || []);
    const imageItems = items.filter(it => it.kind === 'file' && /^image\//.test(it.type));
    if (imageItems.length === 0) return;
    event.preventDefault();
    for (const item of imageItems) {
      const file = item.getAsFile();
      if (!file) continue;
      if (!ALLOWED_MIME.has(file.type)) {
        toast('不支持的图片格式');
        continue;
      }
      if (chips.length >= IMG_MAX) {
        toast(`最多 ${IMG_MAX} 张图片`);
        break;
      }
      const id = 'c' + (++chipSeq);
      const chip = {
        id,
        name: file.name || `paste-${chipSeq}.png`,
        status: 'uploading',
        blob: file,
        mime: file.type,
      };
      chips.push(chip);
      renderChips();
      refreshSubmitState();
      try {
        const meta = await uploadAndRegister(file, file.type, chip.name);
        chip.status = 'ok';
        chip.url = meta.url;
        chip.refId = meta.refId;
        insertRefToken(chip.refId);
      } catch (err) {
        chip.status = 'failed';
        chip.err = (err && err.message) || '上传失败';
      } finally {
        renderChips();
        refreshSubmitState();
      }
    }
  });

  // ── inputs ──
  els.titleInput.addEventListener('input', () => {
    titleTouched = true;
    refreshTitleUI();
  });
  els.titleInput.addEventListener('blur', () => {
    titleTouched = true;
    refreshTitleUI();
  });
  els.contentInput.addEventListener('input', autosizeContent);

  // ── focus trap ──
  function focusableInModal() {
    const sel = 'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
    return Array.from(modal.querySelectorAll(sel)).filter(el => el.offsetParent !== null);
  }

  modal.addEventListener('keydown', e => {
    if (e.key === 'Tab') {
      const f = focusableInModal();
      if (f.length === 0) return;
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      requestClose();
    }
  });

  modal.addEventListener('mousedown', e => {
    // click on backdrop (modal === backdrop wrapper, .tc-modal is the inner)
    if (e.target === modal) {
      requestClose();
    }
  });

  function requestClose() {
    if (submitting) return; // A-12: submitting 期间忽略
    if (hasAnyInput()) {
      if (!confirm('放弃这条 thread？')) return;
    }
    closeModal();
  }

  function openModal() {
    if (isOpen()) return;
    lastFocusedEl = document.activeElement;
    // reset state
    chips = [];
    chipSeq = 0;
    submitting = false;
    titleTouched = false;
    els.titleInput.value = '';
    els.contentInput.value = '';
    els.titleInput.classList.remove('error');
    els.titleErr.textContent = '';
    els.alert.hidden = true;
    els.alert.textContent = '';
    els.btnSubmit.innerHTML = '创建 thread';
    els.btnCancel.disabled = false;
    els.btnClose.disabled = false;
    els.titleInput.disabled = false;
    els.contentInput.disabled = false;
    renderChips();
    refreshTitleUI();
    autosizeContent();
    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
    // focus title after the modal becomes visible
    setTimeout(() => els.titleInput.focus(), 30);
  }

  function closeModal() {
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
    // return focus to ➕
    setTimeout(() => {
      const target = els.btnNew || lastFocusedEl;
      if (target && typeof target.focus === 'function') target.focus();
    }, 0);
  }

  els.btnCancel.addEventListener('click', e => { e.preventDefault(); requestClose(); });
  els.btnClose.addEventListener('click', e => { e.preventDefault(); requestClose(); });

  // ── submit ──
  els.form.addEventListener('submit', async e => {
    e.preventDefault();
    if (submitting) return;
    titleTouched = true;
    refreshTitleUI();
    const { ok, trimmed: title } = validateTitle();
    if (!ok) {
      els.titleInput.focus();
      return;
    }
    if (anyUploading()) {
      toast('图片还在上传…');
      return;
    }
    if (anyFailed()) {
      els.alert.hidden = false;
      els.alert.textContent = '⚠ 请先处理失败的图片（重试或删除）';
      return;
    }
    if (!state.currentSquadId) {
      els.alert.hidden = false;
      els.alert.textContent = '⚠ 请先选择一个 squad';
      return;
    }

    const contentRaw = (els.contentInput.value || '').trim();
    // Keep `[[ref:<id>]]` tokens in the post body — the existing renderer
    // (FILE_LINK_RE → resolveChipFromRefs → ref chip) already turns them into
    // image chips when the ref's content_type starts with `image/`.
    let contentForPost = contentRaw;
    // If user has chips that are uploaded but somehow not represented in the
    // text (e.g. user deleted the token but didn't remove the chip),
    // append their tokens at the end so the image still ships with the post.
    const okChips = chips.filter(c => c.status === 'ok' && c.refId);
    const orphanRefs = okChips
      .filter(c => !contentForPost.includes(`[[ref:${c.refId}]]`))
      .map(c => `[[ref:${c.refId}]]`);
    if (orphanRefs.length) {
      contentForPost = (contentForPost ? contentForPost + '\n\n' : '') + orphanRefs.join('\n');
    }

    submitting = true;
    els.btnSubmit.disabled = true;
    els.btnCancel.disabled = true;
    els.btnClose.disabled = true;
    els.titleInput.disabled = true;
    els.contentInput.disabled = true;
    els.btnSubmit.innerHTML = '<span class="tc-spinner"></span>创建中…';
    els.alert.hidden = true;
    refreshSubmitState();

    let thread = null;
    try {
      thread = await apiJson(
        `/api/squads/${encodeURIComponent(state.currentSquadId)}/threads`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, created_by: 'scott' }),
        }
      );
    } catch (err) {
      submitting = false;
      els.btnSubmit.disabled = false;
      els.btnCancel.disabled = false;
      els.btnClose.disabled = false;
      els.titleInput.disabled = false;
      els.contentInput.disabled = false;
      els.btnSubmit.innerHTML = '创建 thread';
      els.alert.hidden = false;
      els.alert.textContent = `⚠ 创建 thread 失败：${err.message || err}`;
      refreshSubmitState();
      return;
    }

    // step 2: post content (only if non-empty)
    if (contentForPost) {
      try {
        await apiJson(
          `/api/threads/${encodeURIComponent(thread.thread_id)}/posts`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: contentForPost, speaker: 'scott' }),
          }
        );
      } catch (err) {
        // A-11: thread exists, post failed. Jump in + show red banner.
        try { await refreshThreadsForCurrentSquad(); } catch (_) {}
        try { await selectThread(thread.thread_id); } catch (_) {}
        closeModal();
        setStatus(`⚠ thread 已创建，但首条 post 发送失败：${err.message || err}`, false);
        toast('首条内容发送失败，请进入 thread 重发');
        return;
      }
    }

    // success
    try { await refreshThreadsForCurrentSquad(); } catch (_) {}
    try { await selectThread(thread.thread_id); } catch (_) {}
    submitting = false;
    closeModal();
  });

  // ── wire ➕ + ⌘N ──
  if (els.btnNew) {
    els.btnNew.addEventListener('click', e => {
      e.preventDefault();
      openModal();
    });
  }

  document.addEventListener('keydown', e => {
    // ⌘N / Ctrl+N — preventDefault (R-3) to override browser "new window".
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && (e.key === 'n' || e.key === 'N')) {
      // only when home view is visible
      const home = document.getElementById('home-view');
      if (home && !home.hidden) {
        e.preventDefault();
        if (!isOpen()) openModal();
      }
    }
  });

  // expose for debugging
  window.__threadCreateModal = { open: openModal, close: closeModal };
})();

/* ─── v0.6: icon-rail routing + Files view ─────────────────────────────── */
(function () {
  const homeView = document.getElementById('home-view');
  const filesView = document.getElementById('files-view');
  const items = Array.from(document.querySelectorAll('.icon-rail-item'));
  const toastEl = document.getElementById('toast');

  function toast(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { toastEl.hidden = true; }, 1800);
  }

  function setActive(view) {
    items.forEach(it => it.classList.toggle('is-active', it.dataset.view === view));
    const agentsView = document.getElementById('agents-view');
    const activityView = document.getElementById('activity-view');
    const hideAll = () => {
      homeView.hidden = true;
      filesView.hidden = true;
      if (agentsView) agentsView.hidden = true;
      if (activityView) activityView.hidden = true;
    };
    if (view === 'files') {
      hideAll();
      filesView.hidden = false;
      if (state.activeTab === 'favorites' && window.__forgeFavoritesActivate) window.__forgeFavoritesActivate();
    } else if (view === 'agents') {
      hideAll();
      if (agentsView) agentsView.hidden = false;
    } else if (view === 'activity') {
      hideAll();
      if (activityView) activityView.hidden = false;
      if (window.__forgeActivityActivate) window.__forgeActivityActivate();
    } else {
      hideAll();
      homeView.hidden = false;
      if (window.__forgeActivityDeactivate) window.__forgeActivityDeactivate();
    }
  }

  function routeFromHash() {
    const h = location.hash || '';
    if (h.startsWith('#/activity')) {
      setActive('activity');
      const m = h.match(/^#\/activity\/(th_[A-Za-z0-9_]+)$/);
      if (m && window.__forgeActivitySelect) window.__forgeActivitySelect(decodeURIComponent(m[1]));
      return;
    }
    if (h.startsWith('#/favorites')) {
      // PRD v1.2 follow-up (judy review #2): record that a legacy v1
      // bookmark still points at /favorites. Separate source so audit
      // can tell chip exposure apart from stale-bookmark hits.
      try { v07Bump('legacy_favorites_hash'); } catch (_) { /* no-op */ }
      location.replace('#/files/favorites');
      return;
    }
    if (h.startsWith('#/agents')) {
      setActive('agents');
      const m = h.match(/^#\/agents\/([A-Za-z0-9][A-Za-z0-9._\-]{0,63})$/);
      if (m && window.__forgeAgentsSelect) window.__forgeAgentsSelect(decodeURIComponent(m[1]));
      return;
    }
    if (h.startsWith('#/files')) {
      setActive('files');
      // v0.8: #/files/refs/<id>
      let m = h.match(/^#\/files\/refs\/(ref_[A-Za-z0-9]+)$/);
      if (m) {
        switchTab('refs');
        selectRef(decodeURIComponent(m[1]));
        return;
      }
      // v0.8: #/files/refs (list tab)
      if (h === '#/files/refs' || h.startsWith('#/files/refs?')) {
        switchTab('refs');
        return;
      }
      // v2: #/files/favorites — Favorites tab inside FILES panel
      if (h === '#/files/favorites' || h.startsWith('#/files/favorites?')) {
        switchTab('favorites');
        return;
      }
      // Bare /#/files → default tab
      switchTab(state.activeTab || 'refs');
    } else {
      setActive('home');
    }
  }

  // Expose setActive for the agents IIFE
  window.__forgeSetActive = setActive;

  items.forEach(it => {
    it.addEventListener('click', () => {
      const v = it.dataset.view;
      if (it.dataset.enabled === '1') {
        if (v === 'files') location.hash = '#/files';
        else if (v === 'agents') location.hash = '#/agents';
        else if (v === 'activity') location.hash = '#/activity';
        else location.hash = '#/squads';
      } else {
        toast('「' + (it.querySelector('.label')?.textContent || v) + '」敬请期待');
      }
    });
  });
  window.addEventListener('hashchange', routeFromHash);

  /* ── files state ── */
  const state = {
    roots: [],            // [{id, label, writable, count}]
    currentRoot: null,    // root id
    files: [],
    current: null,        // filename string
    content: '',
    dirty: false,
    mode: 'preview',      // 'preview' | 'edit'
    // v0.8
    activeTab: 'refs',    // 'refs' | 'workspace'
    refs: [],             // [{id,label,abs_path,source_agent,...}]
    refSearch: '',
    currentRef: null,     // ref object
  };

  const listEl = document.getElementById('file-list');
  const emptyEl = document.getElementById('file-list-empty');
  const titleEl = document.getElementById('file-title');
  const subEl = document.getElementById('file-sub');
  const previewEl = document.getElementById('file-preview');
  const editorEl = document.getElementById('file-editor');
  // v2: Workspace tab removed; btnNew + rootSelect not in DOM. Variables
  // are nullable; the legacy loadFileList/selectFile path is dead but
  // still imported for #/files/refs/<id> sibling helpers to compile.
  const btnNew = document.getElementById('btn-new-file');
  const rootSelect = document.getElementById('file-root-select');
  const btnToggle = document.getElementById('btn-toggle-edit');
  const btnSave = document.getElementById('btn-save-file');
  // v0.8
  const tabRefs = document.getElementById('files-tab-refs');
  const tabFavorites = document.getElementById('files-tab-favorites');
  const refsPane = document.getElementById('files-refs-pane');
  const favPane = document.getElementById('files-favorites-pane');
  const refsListEl = document.getElementById('refs-list');
  const refsEmptyEl = document.getElementById('refs-empty');
  const refsSearchEl = document.getElementById('refs-search');

  function switchTab(tab) {
    state.activeTab = tab;
    const isRefs = tab === 'refs';
    const isFav = tab === 'favorites';
    if (tabRefs) tabRefs.classList.toggle('is-active', isRefs);
    if (tabFavorites) tabFavorites.classList.toggle('is-active', isFav);
    if (refsPane) refsPane.hidden = !isRefs;
    if (favPane) favPane.hidden = !isFav;
    if (isRefs) {
      loadRefs();
    } else if (isFav && window.__forgeFavoritesActivate) {
      window.__forgeFavoritesActivate();
    }
  }

  if (tabRefs) tabRefs.addEventListener('click', () => { location.hash = '#/files/refs'; });
  if (tabFavorites) tabFavorites.addEventListener('click', () => { location.hash = '#/files/favorites'; });
  if (refsSearchEl) refsSearchEl.addEventListener('input', () => {
    state.refSearch = refsSearchEl.value || '';
    renderRefsList();
  });

  async function loadRefs() {
    try {
      const r = await fetch('/api/refs');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      state.refs = Array.isArray(data.refs) ? data.refs : [];
      // share with the chip resolver
      if (window._forgeRefs) {
        window._forgeRefs.all = state.refs;
        window._forgeRefs.byId = new Map(state.refs.map(r => [r.id, r]));
        window._forgeRefs.loaded = true;
      }
      renderRefsList();
    } catch (e) {
      state.refs = [];
      renderRefsList();
      toast('加载引用列表失败');
    }
  }

  function renderRefsList() {
    if (!refsListEl) return;
    refsListEl.innerHTML = '';
    const q = (state.refSearch || '').toLowerCase().trim();
    const filtered = q
      ? state.refs.filter(r => (r.label || '').toLowerCase().includes(q)
          || (r.source_agent || '').toLowerCase().includes(q))
      : state.refs;
    if (!filtered.length) {
      refsEmptyEl.hidden = false;
      refsEmptyEl.textContent = q
        ? '没有匹配“' + q + '”的引用。'
        : '还没有 agent 注册过文件引用。';
      return;
    }
    refsEmptyEl.hidden = true;
    // group by source_agent
    const groups = new Map();
    for (const r of filtered) {
      const key = r.source_agent || 'unknown';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    }
    if (!state.refsCollapsed) state.refsCollapsed = new Set();
    for (const [agent, refs] of groups) {
      const collapsed = state.refsCollapsed.has(agent);
      const head = document.createElement('li');
      head.className = 'refs-group-head' + (collapsed ? ' is-collapsed' : '');
      head.textContent = agent + ' (' + refs.length + ')';
      head.addEventListener('click', () => {
        if (state.refsCollapsed.has(agent)) state.refsCollapsed.delete(agent);
        else state.refsCollapsed.add(agent);
        renderRefsList();
      });
      refsListEl.appendChild(head);
      if (collapsed) continue;
      for (const r of refs) {
        const li = document.createElement('li');
        li.className = 'refs-item';
        if (state.currentRef && state.currentRef.id === r.id) li.classList.add('is-active');
        li.title = r.abs_path;
        li.innerHTML = '<span class="refs-item-label">' + escapeHtml(r.label) + '</span>'
          + '<span class="refs-item-meta">' + fmtSize(r.size_hint || 0) + ' · ' + fmtTime(r.registered_at) + '</span>';
        li.addEventListener('click', () => {
          location.hash = '#/files/refs/' + encodeURIComponent(r.id);
        });
        refsListEl.appendChild(li);
      }
    }
  }

  async function selectRef(refId) {
    try {
      if (!state.refs.length) await loadRefs();
      let ref = state.refs.find(r => r.id === refId);
      if (!ref) {
        // Refresh in case we registered very recently
        const meta = await fetch('/api/refs/' + encodeURIComponent(refId));
        if (!meta.ok) { toast('引用不存在: ' + refId); return; }
        ref = await meta.json();
        state.refs = [ref, ...state.refs.filter(r => r.id !== ref.id)];
      }
      state.currentRef = ref;
      state.current = null;
      state.dirty = false;
      state.mode = 'preview';
      state.refEtag = null;
      state.isMdRef = /\.md$/i.test(ref.label || '');
      titleEl.textContent = ref.label;
      subEl.textContent = (ref.source_agent ? ref.source_agent + ' · ' : '')
        + fmtSize(ref.size_hint || 0) + ' · 注册于 ' + fmtTime(ref.registered_at)
        + ' · ' + ref.abs_path;
      // PRD v1.2 viewer entry (alice 20:32 / designer 20:38): viewer header
      // ⭐ button. State pulled from window._forgeFavSet (already populated
      // by Favorites tab bootstrap); no extra GET needed.
      if (typeof setViewerFavTarget === 'function') {
        setViewerFavTarget({ abs_path: ref.abs_path, ref_id: ref.id,
          source_agent: ref.source_agent, thread_id: ref.thread_id });
      }
      previewEl.innerHTML = '<p class="meta">加载中…</p>';
      editorEl.hidden = true;
      previewEl.hidden = false;
      btnSave.hidden = true;
      btnToggle.disabled = true;
      btnToggle.textContent = '编辑';
      btnToggle.title = state.isMdRef ? '' : 'v1 仅支持 .md 文件编辑';
      const r = await fetch('/api/refs/' + encodeURIComponent(refId) + '/content');
      if (!r.ok) {
        previewEl.innerHTML = '<p class="meta">加载失败: HTTP ' + r.status + '</p>';
        renderRefsList();
        return;
      }
      state.refEtag = r.headers.get('ETag') || r.headers.get('X-Ref-Etag') || null;
      const ctype = (r.headers.get('Content-Type') || '').toLowerCase();
      const buf = await r.arrayBuffer();
      if (ctype.startsWith('image/')) {
        const blob = new Blob([buf], { type: ctype });
        const url = URL.createObjectURL(blob);
        previewEl.innerHTML = '<img alt="' + escapeAttr(ref.label) + '" src="' + url + '" style="max-width:100%;height:auto" />';
      } else {
        const text = new TextDecoder().decode(buf);
        state.content = text;
        if ((ctype.startsWith('text/markdown') || /\.md$/i.test(ref.label)) && typeof marked !== 'undefined' && marked.parse) {
          previewEl.innerHTML = openExternalLinksInNewTab(marked.parse(text));
          renderMermaidIn(previewEl);
        } else if (ctype.includes('html') || /\.x?html?$/i.test(ref.label)) {
          // Render HTML in a sandboxed iframe (no scripts/forms/top-nav).
          // Fixes: HTML refs (e.g. designer mocks) were shown as escaped
          // source instead of being previewed. sandbox="" = strictest.
          const iframe = document.createElement('iframe');
          iframe.setAttribute('sandbox', '');
          iframe.setAttribute('srcdoc', text);
          iframe.style.cssText = 'width:100%;height:calc(100vh - 180px);min-height:480px;border:1px solid var(--border);border-radius:6px;background:#fff;';
          iframe.title = ref.label || 'html preview';
          previewEl.innerHTML = '';
          previewEl.appendChild(iframe);
        } else if (ctype.includes('json')) {
          try {
            previewEl.innerHTML = '<pre>' + escapeHtml(JSON.stringify(JSON.parse(text), null, 2)) + '</pre>';
          } catch (_) {
            previewEl.innerHTML = '<pre>' + escapeHtml(text) + '</pre>';
          }
        } else {
          previewEl.innerHTML = '<pre>' + escapeHtml(text) + '</pre>';
        }
        editorEl.value = text;
        if (state.isMdRef) {
          btnToggle.disabled = false;
        }
      }
      renderRefsList();
    } catch (e) {
      toast('打开引用失败');
    }
  }

  function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleString();
  }
  function fmtSize(n) {
    if (n < 1024) return n + ' B';
    return (n / 1024).toFixed(1) + ' KB';
  }

  function currentRootMeta() {
    return state.roots.find(r => r.id === state.currentRoot) || null;
  }

  async function loadRoots() {
    try {
      const r = await fetch('/api/file-roots');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      state.roots = data.roots || [];
    } catch (e) {
      // fallback: synthesise a single 'files' root
      state.roots = [{ id: 'files', label: 'Files', writable: true, count: 0 }];
    }
    if (!state.currentRoot || !state.roots.find(r => r.id === state.currentRoot)) {
      state.currentRoot = state.roots[0]?.id || 'files';
    }
    renderRootSelect();
  }

  function renderRootSelect() {
    if (!rootSelect) return;
    rootSelect.innerHTML = '';
    for (const r of state.roots) {
      const opt = document.createElement('option');
      opt.value = r.id;
      opt.textContent = `${r.label}${r.writable ? '' : ' 🔒'} (${r.count})`;
      if (r.id === state.currentRoot) opt.selected = true;
      rootSelect.appendChild(opt);
    }
  }

  if (rootSelect) {
    rootSelect.addEventListener('change', () => {
      const newRoot = rootSelect.value;
      if (state.dirty && !confirm('当前文件未保存，切换会丢失改动。继续？')) {
        rootSelect.value = state.currentRoot;
        return;
      }
      state.currentRoot = newRoot;
      clearSelection();
      location.hash = '#/files/' + encodeURIComponent(newRoot);
      loadFileList();
    });
  }

  async function loadFileList() {
    try {
      if (!state.roots.length) await loadRoots();
      const root = state.currentRoot || (state.roots[0]?.id) || 'files';
      state.currentRoot = root;
      const r = await fetch('/api/files?root=' + encodeURIComponent(root));
      const data = await r.json();
      state.files = data.files || [];
      // refresh counts on the selector
      const meta = state.roots.find(x => x.id === root);
      if (meta) meta.count = state.files.length;
      renderRootSelect();
      renderList();
      if (state.current && !state.files.find(f => f.name === state.current)) {
        clearSelection();
      }
    } catch (e) {
      toast('加载文件失败');
    }
  }

  function renderList() {
    listEl.innerHTML = '';
    const meta = currentRootMeta();
    if (!state.files.length) {
      emptyEl.hidden = false;
      emptyEl.textContent = meta && !meta.writable
        ? '这个目录是只读的，且没有文件。'
        : '还没有 md 文件，点上面 + 新建 一个吧。';
      return;
    }
    emptyEl.hidden = true;
    for (const f of state.files) {
      const li = document.createElement('li');
      li.textContent = f.name.replace(/\.md$/, '');
      li.title = `${f.name} · ${fmtSize(f.size)} · ${fmtTime(f.mtime)}`;
      li.dataset.name = f.name;
      if (f.name === state.current) li.classList.add('is-active');
      li.addEventListener('click', () => {
        if (state.dirty && !confirm('当前文件未保存，切换会丢失改动。继续？')) return;
        location.hash = '#/files/' + encodeURIComponent(state.currentRoot) + '/' + f.name;
      });
      listEl.appendChild(li);
    }
  }

  function clearSelection() {
    state.current = null;
    state.content = '';
    state.dirty = false;
    state.mode = 'preview';
    titleEl.textContent = '选择一个文件';
    subEl.textContent = '';
    previewEl.innerHTML = '';
    editorEl.value = '';
    editorEl.hidden = true;
    previewEl.hidden = false;
    btnToggle.disabled = true;
    btnToggle.textContent = '编辑';
    btnSave.hidden = true;
    btnSave.disabled = true;
    if (typeof setViewerFavTarget === 'function') setViewerFavTarget(null);
  }

  async function selectFile(name, rootId) {
    try {
      if (!state.roots.length) await loadRoots();
      // If a root was specified in the URL, switch to it first.
      if (rootId && rootId !== state.currentRoot) {
        if (!state.roots.find(r => r.id === rootId)) {
          toast('未知目录: ' + rootId);
          return;
        }
        state.currentRoot = rootId;
        await loadFileList();
      } else if (!rootId && !state.currentRoot) {
        await loadFileList();
      }
      const root = state.currentRoot;
      const url = '/api/files/' + encodeURIComponent(root) + '/' + encodeURIComponent(name);
      const r = await fetch(url);
      if (!r.ok) { toast('打开失败 ' + r.status); return; }
      const data = await r.json();
      state.current = name;
      state.content = data.content || '';
      state.dirty = false;
      state.mode = 'preview';
      // Workspace-file mode: clear any ref-edit state so saveCurrent()
      // doesn't mis-route a workspace save into the ref endpoint of a
      // previously-opened ref (codex review PR#48 🔴).
      state.currentRef = null;
      state.refEtag = null;
      state.isMdRef = false;
      const meta = currentRootMeta();
      const readOnly = meta && !meta.writable;
      titleEl.textContent = name + (readOnly ? '  🔒' : '');
      subEl.textContent = `${fmtSize(data.size)} · 修改于 ${fmtTime(data.mtime)}` + (readOnly ? ' · 只读' : '');
      editorEl.value = state.content;
      renderPreview();
      setMode('preview');
      btnToggle.disabled = !!readOnly;
      btnToggle.title = readOnly ? '此目录只读' : '';
      renderList();
      renderRootSelect();
    } catch (e) {
      toast('加载文件失败');
    }
  }

  function renderPreview() {
    if (typeof marked !== 'undefined' && marked.parse) {
      previewEl.innerHTML = openExternalLinksInNewTab(marked.parse(state.content || ''));
      renderMermaidIn(previewEl);
    } else {
      previewEl.textContent = state.content || '';
    }
  }

  // ── v1.1: ref edit (.md only) ────────────────────────────────────
  const DANGER_BASENAMES = new Set([
    'STATUS.md', 'MEMORY.md', 'AGENTS.md',
    'SOUL.md', 'IDENTITY.md', 'USER.md',
  ]);
  function refAgentFromPath(absPath) {
    if (!absPath) return null;
    // Match a path segment like '/workspace-<agent>/...'. Agent ids
    // mirror the server allowlist (letters/digits/_/-/.).
    const m = String(absPath).match(/(?:^|\/)workspace-([A-Za-z0-9_.\-]+)(?=\/|$)/);
    return m ? m[1] : null;
  }
  function basenameOf(p) {
    if (!p) return '';
    const s = String(p).split('/');
    return s[s.length - 1];
  }
  function ensureDangerBanner() {
    let el = document.getElementById('ref-danger-banner');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'ref-danger-banner';
    el.setAttribute('role', 'alert');
    el.style.cssText = 'background:#FEF3C7;color:#92400E;border-top:1px solid #FBBF24;border-bottom:1px solid #FBBF24;padding:10px 16px;font-size:13px;line-height:1.4;display:none';
    editorEl.parentNode.insertBefore(el, editorEl);
    return el;
  }
  function updateDangerBanner() {
    const banner = ensureDangerBanner();
    const ref = state.currentRef;
    if (!ref || !state.isMdRef || state.mode !== 'edit') {
      banner.style.display = 'none';
      return;
    }
    const base = basenameOf(ref.abs_path || ref.label || '');
    if (!DANGER_BASENAMES.has(base)) {
      banner.style.display = 'none';
      return;
    }
    const agent = refAgentFromPath(ref.abs_path) || ref.source_agent || 'agent';
    banner.textContent = `⚠ 这是 ${agent} 的自维护文件（${base}），保存会让 ${agent} 下一 turn 行为漂移。确认要改。`;
    banner.style.display = 'block';
  }

  async function saveRef() {
    const ref = state.currentRef;
    if (!ref || !state.dirty || !state.isMdRef) return;
    btnSave.disabled = true;
    const origLabel = btnSave.textContent;
    btnSave.textContent = '保存中…';
    editorEl.readOnly = true;
    const actor = 'scott';
    const threadId = state.currentThreadId || null;
    const body = { content: state.content, actor, thread_id: threadId };
    const headers = { 'Content-Type': 'application/json' };
    if (state.refEtag) headers['If-Match'] = state.refEtag;
    let resp;
    try {
      resp = await fetch('/api/refs/' + encodeURIComponent(ref.id) + '/content', {
        method: 'PUT', headers, body: JSON.stringify(body),
      });
    } catch (e) {
      editorEl.readOnly = false;
      btnSave.disabled = false;
      btnSave.textContent = origLabel || '保存';
      toast('网络错误，未保存（本地改动已保留）');
      return;
    }
    editorEl.readOnly = false;
    btnSave.textContent = origLabel || '保存';
    if (resp.status === 409) {
      const data = await resp.json().catch(() => ({}));
      const remote = data.current_etag || '?';
      const local = state.refEtag || '?';
      const choice = confirm(
        `文件在远端已变更：\n  远端 etag ${remote}\n  本地 etag ${local}\n\n点「确定」= 用我的改动强制覆盖；点「取消」= 放弃我的改动，加载远端版本。`
      );
      if (choice) {
        state.refEtag = remote;
        btnSave.disabled = false;
        state.dirty = true;
        return saveRef();
      } else {
        state.content = data.current_content || '';
        state.refEtag = remote;
        editorEl.value = state.content;
        state.dirty = false;
        btnSave.disabled = true;
        renderPreview();
        return;
      }
    }
    if (resp.status === 413) { toast('文件超过 1MB，本编辑器不支持。请用 IDE 改。'); btnSave.disabled = false; return; }
    if (resp.status === 404) { toast('文件已不存在（404）'); btnSave.disabled = false; return; }
    if (resp.status === 403) { toast('此文件不可编辑（403）'); btnSave.disabled = false; return; }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      toast('保存失败 ' + resp.status + ' ' + (err.error || ''));
      btnSave.disabled = false;
      return;
    }
    const data = await resp.json();
    state.refEtag = data.etag || null;
    state.dirty = false;
    subEl.textContent = (ref.source_agent ? ref.source_agent + ' · ' : '')
      + fmtSize(data.size || 0) + ' · 修改于 ' + fmtTime(data.mtime)
      + ' · ' + (ref.abs_path || '');
    ref.size_hint = data.size;
    toast('✓ 已保存 ' + new Date().toLocaleTimeString());
    setMode('preview');
    renderPreview();
  }

  function setMode(mode) {
    state.mode = mode;
    if (mode === 'edit') {
      previewEl.hidden = true;
      editorEl.hidden = false;
      btnToggle.textContent = '预览';
      btnSave.hidden = false;
      btnSave.disabled = !state.dirty;
      editorEl.focus();
    } else {
      editorEl.hidden = true;
      previewEl.hidden = false;
      btnToggle.textContent = '编辑';
      btnSave.hidden = true;
    }
    if (typeof updateDangerBanner === 'function') updateDangerBanner();
  }

  btnToggle.addEventListener('click', () => {
    // Either editing a workspace file (state.current) or a ref (state.currentRef).
    if (!state.current && !state.currentRef) return;
    setMode(state.mode === 'preview' ? 'edit' : 'preview');
    updateDangerBanner();
  });

  editorEl.addEventListener('input', () => {
    state.content = editorEl.value;
    state.dirty = true;
    btnSave.disabled = false;
    renderPreview();
  });

  // IME guard: ⌘/Ctrl+S during a composition should NOT submit (avoid
  // saving half-typed Chinese input).
  let _imeComposing = false;
  editorEl.addEventListener('compositionstart', () => { _imeComposing = true; });
  editorEl.addEventListener('compositionend', () => { _imeComposing = false; });

  async function saveCurrent() {
    // Ref path takes precedence when the right pane is showing a ref.
    if (state.currentRef && state.isMdRef) {
      return saveRef();
    }
    if (!state.current || !state.dirty) return;
    const meta = currentRootMeta();
    if (meta && !meta.writable) { toast('该目录只读'); return; }
    btnSave.disabled = true;
    try {
      const root = state.currentRoot;
      const url = '/api/files/' + encodeURIComponent(root) + '/' + encodeURIComponent(state.current);
      const r = await fetch(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: state.content }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        toast('保存失败 ' + r.status + ' ' + (err.error || ''));
        btnSave.disabled = false;
        return;
      }
      const data = await r.json();
      state.dirty = false;
      subEl.textContent = `${fmtSize(data.size)} · 修改于 ${fmtTime(data.mtime)}`;
      toast('已保存');
      setMode('preview');
      loadFileList();
    } catch (e) {
      toast('保存失败');
      btnSave.disabled = false;
    }
  }
  btnSave.addEventListener('click', saveCurrent);

  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 's' && !filesView.hidden && state.mode === 'edit') {
      // Always swallow ⌘/Ctrl+S so the browser's "save page" dialog
      // never appears in this view. If the user is mid-IME composition
      // we skip the actual save (don't commit half-typed Chinese).
      e.preventDefault();
      if (_imeComposing) return;
      saveCurrent();
    }
  });

  window.addEventListener('beforeunload', (e) => {
    if (state.dirty) { e.preventDefault(); e.returnValue = ''; }
  });

  if (btnNew) btnNew.addEventListener('click', async () => {
    const meta = currentRootMeta();
    if (meta && !meta.writable) { toast('此目录只读'); return; }
    let name = prompt('新文件名（必须以 .md 结尾，只能是字母数字 _ - .）：', 'untitled.md');
    if (!name) return;
    name = name.trim();
    if (!/^[A-Za-z0-9_.\-]+\.md$/.test(name) || name.startsWith('.')) {
      toast('文件名非法');
      return;
    }
    try {
      const root = state.currentRoot || 'files';
      const r = await fetch('/api/files/' + encodeURIComponent(root), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, content: '' }),
      });
      if (r.status === 403) { toast('此目录只读'); return; }
      if (r.status === 409) { toast('已存在同名文件'); return; }
      if (!r.ok) { toast('创建失败 ' + r.status); return; }
      await loadFileList();
      location.hash = '#/files/' + encodeURIComponent(root) + '/' + name;
      setTimeout(() => setMode('edit'), 50);
    } catch (e) {
      toast('创建失败');
    }
  });

  // initial routing
  // v2: Workspace tab removed — no need to preload fileRoots at boot.
  // Just warm the ref index for chip resolution + run router.
  loadRefIndex().then(routeFromHash);
})();

/* ─── v0.9: Agents view (STATUS + context-bundle preview) ────────────────── */
(function () {
  const view = document.getElementById('agents-view');
  if (!view) return;
  const input = document.getElementById('agents-id-input');
  const list = document.getElementById('agents-list');
  const empty = document.getElementById('agents-empty');
  const title = document.getElementById('agent-title');
  const sub = document.getElementById('agent-sub');
  const statusCard = document.getElementById('agent-status-card');
  const bundleCard = document.getElementById('agent-bundle-card');
  const bundlePre = document.getElementById('agent-bundle-pre');
  const refreshBtn = document.getElementById('btn-agent-bundle-refresh');
  const agentsRefreshBtn = document.getElementById('btn-agents-refresh');
  const emptyMain = document.getElementById('agent-empty');
  const avatarControl = document.getElementById('agent-avatar-control');
  const avatarBtn = document.getElementById('btn-agent-avatar');
  const avatarPreview = document.getElementById('agent-avatar-preview');

  const knownAgents = new Set();
  let current = null;

  function normalizeId(s) {
    return String(s || '').trim().toLowerCase();
  }

  function addAgent(id) {
    const n = normalizeId(id);
    if (n) knownAgents.add(n);
  }

  function renderList() {
    list.innerHTML = '';
    const ids = Array.from(knownAgents).sort();
    if (!ids.length) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    for (const id of ids) {
      const li = document.createElement('li');
      li.className = 'agents-item' + (id === current ? ' is-active' : '');
      li.innerHTML = `${renderAgentListAvatar(id)}<span class="agents-item-name">${escapeHtml(id)}</span>`;
      li.addEventListener('click', () => { location.hash = '#/agents/' + encodeURIComponent(id); });
      list.appendChild(li);
    }
  }

  function renderAgentListAvatar(id) {
    const av = window.OpenForgeAvatar?.getDefaultAvatar?.(id, '') || { pngPath: '', glyph: id.slice(0, 1).toUpperCase() };
    const img = av.pngPath
      ? '<img class="avatar-img" src="' + escapeHtml(av.pngPath) + '" alt="" aria-hidden="true" loading="lazy" />'
      : '';
    return '<span class="mini-avatar avatar-default agents-list-avatar">'
      + img + '<span class="avatar-glyph">' + escapeHtml(av.glyph || '?') + '</span></span>';
  }

  async function discoverAgents() {
    // Primary source: /api/agents (union of squad members + ~/.openclaw/agents/*).
    // Falls back to /api/refs source_agent if the new endpoint is missing.
    try {
      const r = await fetch('/api/agents');
      if (r.ok) {
        const d = await r.json();
        if (Array.isArray(d)) d.forEach(addAgent);
      }
    } catch (e) { /* graceful */ }
    try {
      const r = await fetch('/api/refs');
      if (r.ok) {
        const d = await r.json();
        (d.refs || []).forEach(ref => { if (ref.source_agent) addAgent(ref.source_agent); });
      }
    } catch (e) { /* graceful */ }
    renderList();
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  async function selectAgent(agentId) {
    current = normalizeId(agentId);
    if (current) addAgent(current);
    renderList();
    title.textContent = current ? '🧑 ' + current : '选择一个 agent';
    sub.textContent = '';
    statusCard.hidden = true;
    bundleCard.hidden = true;
    emptyMain.hidden = !current;
    refreshBtn.disabled = !current;
    if (avatarControl) avatarControl.hidden = !current;
    if (avatarBtn) avatarBtn.disabled = !current;
    renderAgentHeaderAvatar();
    if (!current) return;
    await loadAgentAvatarMeta(current);
    // 1) STATUS — render as markdown (mirrors Files view's preview).
    try {
      const r = await fetch('/api/agents/' + encodeURIComponent(current) + '/status');
      if (r.ok) {
        const d = await r.json();
        const updated = new Date((d.updated_at || 0) * 1000).toLocaleString();
        sub.textContent = '更新于 ' + updated + ' · ' + d.size + ' B';
        const md = d.content || '';
        if (typeof marked !== 'undefined' && marked.parse) {
          statusCard.innerHTML = openExternalLinksInNewTab(marked.parse(md));
          renderMermaidIn(statusCard);
        } else {
          statusCard.textContent = md;
        }
        statusCard.hidden = false;
      } else if (r.status === 404) {
        statusCard.innerHTML = '<p class="files-empty">该 agent 还没写过 STATUS.md。</p>';
        statusCard.hidden = false;
      }
    } catch (e) { /* graceful */ }
    // 2) Bundle preview
    await refreshBundle(current, false);
  }

  async function refreshBundle(agentId, force) {
    try {
      const url = '/api/agents/' + encodeURIComponent(agentId) + '/context-bundle' + (force ? '?refresh=1' : '');
      const r = await fetch(url);
      if (!r.ok) return;
      const d = await r.json();
      bundleCard.hidden = false;
      const gen = new Date((d.generated_at || 0) * 1000).toLocaleString();
      const hit = d.cache_hit ? ' (cache hit)' : ' (fresh)';
      bundlePre.textContent =
        'generated_at: ' + gen + hit +
        '\nsize_bytes: ' + d.size_bytes +
        '\nsources: ' + Object.keys(d.sources || {}).join(', ') +
        '\n\n─── rendered ───\n' + (d.rendered || '(empty)');
    } catch (e) { /* graceful */ }
  }

  async function loadAgentAvatarMeta(agentId) {
    try {
      const r = await fetch('/api/agents/' + encodeURIComponent(agentId) + '/avatar?meta=1');
      if (r.ok) {
        const d = await r.json();
        window.__refreshAgentAvatar?.(agentId, d.url || ('/api/agents/' + encodeURIComponent(agentId) + '/avatar?v=' + Date.now()));
      } else if (r.status === 404) {
        window.__refreshAgentAvatar?.(agentId, null);
      }
    } catch (e) { /* keep current/default */ }
  }

  input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const v = normalizeId(input.value);
      if (v) location.hash = '#/agents/' + encodeURIComponent(v);
    }
  });
  refreshBtn?.addEventListener('click', () => { if (current) refreshBundle(current, true); });
  agentsRefreshBtn?.addEventListener('click', discoverAgents);
  avatarBtn?.addEventListener('click', () => { if (current) window.__openAvatarEditor?.(current); });

  function renderAgentHeaderAvatar() {
    if (!avatarPreview) return;
    if (!current) {
      avatarPreview.innerHTML = '';
      return;
    }
    const av = window.OpenForgeAvatar?.getDefaultAvatar?.(current, '') || { pngPath: '', glyph: current.slice(0, 1).toUpperCase() };
    const customUrl = window.__agentAvatarUrls?.get?.(current);
    const src = customUrl || av.pngPath;
    avatarPreview.innerHTML = (src ? '<img class="avatar-img" src="' + escapeHtml(src) + '" alt="" aria-hidden="true" />' : '')
      + (customUrl ? '' : '<span class="avatar-glyph">' + escapeHtml(av.glyph || '?') + '</span>');
  }

  window.__refreshAgentAvatar = (agentId, url) => {
    if (!window.__agentAvatarUrls) window.__agentAvatarUrls = new Map();
    if (url) window.__agentAvatarUrls.set(agentId, url);
    else window.__agentAvatarUrls.delete(agentId);
    if (agentId === current) renderAgentHeaderAvatar();
  };

  window.__forgeAgentsSelect = selectAgent;

  // initial discovery
  discoverAgents();
})();

/* ─── agent avatar upload/crop UI ───────────────────────────────────── */
(function () {
  const modal = document.getElementById('avatar-modal');
  if (!modal) return;
  const confirmModal = document.getElementById('avatar-confirm');
  const canvas = document.getElementById('avatar-canvas');
  const ctx = canvas?.getContext('2d');
  const dropzone = document.getElementById('avatar-dropzone');
  const input = document.getElementById('avatar-file-input');
  const zoom = document.getElementById('avatar-zoom');
  const btnSave = document.getElementById('btn-avatar-save');
  const btnReset = document.getElementById('btn-avatar-reset');
  const btnCancel = document.getElementById('btn-avatar-cancel');
  const btnClose = document.getElementById('btn-avatar-close');
  const btnConfirmClose = document.getElementById('btn-avatar-confirm-close');
  const btnConfirmCancel = document.getElementById('btn-avatar-confirm-cancel');
  const btnConfirmReset = document.getElementById('btn-avatar-confirm-reset');
  const MAX_BYTES = 10 * 1024 * 1024;
  const TOASTS = {
    badFormat: '⚠️ 不支持的格式，仅支持 jpg / png / webp',
    tooBig: (mb) => `⚠️ 图片超过 10MB（当前 ${mb}MB），请压缩后重试`,
    loaded: '拖动调整位置，确认后保存',
    saved: '✅ 头像已更新',
    saveFailed: '❌ 写盘失败，UI 已回滚到旧头像',
    reset: '✅ 已恢复默认头像',
    resetFailed: '❌ 恢复默认失败，请重试',
  };
  let agentId = '';
  let img = null;
  let pos = { x: 128, y: 128 };
  let scale = 1;
  let dragging = false;
  let dragStart = null;

  function setCropState(state) {
    if (dropzone) dropzone.dataset.state = state;
  }

  function drawDefault() {
    if (!ctx) return;
    ctx.clearRect(0, 0, 256, 256);
    ctx.fillStyle = '#eef0f3';
    ctx.fillRect(0, 0, 256, 256);
    ctx.fillStyle = '#8d8d8d';
    ctx.font = '600 14px Inter, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('点击或拖入图片（jpg / png / webp、≤10MB）', 128, 132);
  }

  function drawImage() {
    if (!ctx || !img) return;
    ctx.clearRect(0, 0, 256, 256);
    ctx.save();
    ctx.beginPath();
    ctx.arc(128, 128, 128, 0, Math.PI * 2);
    ctx.clip();
    const base = Math.max(256 / img.width, 256 / img.height);
    const w = img.width * base * scale;
    const h = img.height * base * scale;
    ctx.drawImage(img, pos.x - w / 2, pos.y - h / 2, w, h);
    ctx.restore();
  }

  function loadFile(file) {
    if (!file) return;
    if (!/^image\/(png|jpeg|webp)$/.test(file.type || '')) {
      setCropState('failed');
      showToast(TOASTS.badFormat);
      return;
    }
    if (file.size > MAX_BYTES) {
      setCropState('failed');
      showToast(TOASTS.tooBig((file.size / 1024 / 1024).toFixed(1)));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const next = new Image();
      next.onload = () => {
        img = next;
        pos = { x: 128, y: 128 };
        scale = 1;
        zoom.value = '1';
        btnSave.disabled = false;
        setCropState('default');
        drawImage();
        showToast(TOASTS.loaded);
      };
      next.onerror = () => { setCropState('failed'); showToast(TOASTS.badFormat); };
      next.src = String(reader.result || '');
    };
    reader.onerror = () => { setCropState('failed'); showToast(TOASTS.badFormat); };
    reader.readAsDataURL(file);
  }

  function open(id) {
    agentId = id;
    img = null;
    btnSave.disabled = true;
    btnReset.disabled = !window.__agentAvatarUrls?.has?.(id);
    setCropState('default');
    drawDefault();
    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
  }

  function close() {
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
    setCropState('default');
  }

  function openConfirm() {
    if (btnReset.disabled) return;
    setCropState('confirm');
    confirmModal.classList.add('open');
    confirmModal.setAttribute('aria-hidden', 'false');
  }

  function closeConfirm() {
    confirmModal.classList.remove('open');
    confirmModal.setAttribute('aria-hidden', 'true');
    setCropState('default');
  }

  async function save() {
    if (!agentId || !img) return;
    setCropState('uploading');
    btnSave.disabled = true;
    try {
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
      if (!blob) throw new Error('canvas export failed');
      const res = await fetch('/api/agents/' + encodeURIComponent(agentId) + '/avatar', {
        method: 'POST',
        headers: { 'Content-Type': 'image/png', 'X-OpenForge-UI': '1' },
        body: blob,
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json().catch(() => ({}));
      const url = data.url || ('/api/agents/' + encodeURIComponent(agentId) + '/avatar?v=' + Date.now());
      window.__refreshAgentAvatar?.(agentId, url);
      showToast(TOASTS.saved);
      close();
    } catch (e) {
      setCropState('failed');
      btnSave.disabled = false;
      showToast(TOASTS.saveFailed);
    }
  }

  async function reset() {
    if (!agentId) return;
    try {
      const res = await fetch('/api/agents/' + encodeURIComponent(agentId) + '/avatar', {
        method: 'DELETE',
        headers: { 'X-OpenForge-UI': '1' },
      });
      if (!res.ok && res.status !== 404) throw new Error('HTTP ' + res.status);
      window.__refreshAgentAvatar?.(agentId, null);
      showToast(TOASTS.reset);
      closeConfirm();
      close();
    } catch (e) {
      showToast(TOASTS.resetFailed);
    }
  }

  dropzone?.addEventListener('click', () => input?.click());
  dropzone?.addEventListener('dragover', e => { e.preventDefault(); setCropState('dragging'); });
  dropzone?.addEventListener('dragleave', () => setCropState('default'));
  dropzone?.addEventListener('drop', e => {
    e.preventDefault();
    setCropState('default');
    loadFile(e.dataTransfer?.files?.[0]);
  });
  input?.addEventListener('change', () => loadFile(input.files?.[0]));
  zoom?.addEventListener('input', () => {
    scale = Number(zoom.value || 1);
    setCropState(scale >= 2.99 ? 'max' : 'default');
    drawImage();
  });
  canvas?.addEventListener('pointerdown', e => {
    if (!img) return;
    dragging = true;
    dragStart = { x: e.clientX, y: e.clientY, pos: { ...pos } };
    canvas.setPointerCapture(e.pointerId);
    setCropState('dragging');
  });
  canvas?.addEventListener('pointermove', e => {
    if (!dragging || !dragStart) return;
    pos.x = dragStart.pos.x + (e.clientX - dragStart.x);
    pos.y = dragStart.pos.y + (e.clientY - dragStart.y);
    drawImage();
  });
  canvas?.addEventListener('pointerup', e => {
    dragging = false;
    dragStart = null;
    try { canvas.releasePointerCapture(e.pointerId); } catch {}
    setCropState('default');
  });
  btnSave?.addEventListener('click', save);
  btnReset?.addEventListener('click', openConfirm);
  btnCancel?.addEventListener('click', close);
  btnClose?.addEventListener('click', close);
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
  btnConfirmClose?.addEventListener('click', closeConfirm);
  btnConfirmCancel?.addEventListener('click', closeConfirm);
  btnConfirmReset?.addEventListener('click', reset);
  confirmModal?.addEventListener('click', e => { if (e.target === confirmModal) closeConfirm(); });

  window.__openAvatarEditor = open;
})();

/* ─── v0.5+: Activity view ────────────────────────────────────────────── */
(function () {
  const POLL_MS = 10000;
  const view = document.getElementById('activity-view');
  if (!view) return;
  const listEl = document.getElementById('activity-list');
  const emptyEl = document.getElementById('activity-empty');
  const loadingEl = document.getElementById('activity-loading');
  const countEl = document.getElementById('activity-count');
  const errorBanner = document.getElementById('activity-error-banner');
  const offlineBanner = document.getElementById('activity-offline-banner');
  const newChip = document.getElementById('activity-new-chip');
  const detailEmpty = document.getElementById('activity-detail-empty');
  const detailLoading = document.getElementById('activity-detail-loading');
  const detailError = document.getElementById('activity-detail-error');
  const paneMount = document.getElementById('activity-pane-mount');
  const chips = Array.from(view.querySelectorAll('.activity-chip'));
  const iconRailBtn = document.querySelector('.icon-rail-item[data-view="activity"]');
  const iconRailDot = iconRailBtn?.querySelector('.icon-rail-dot') || null;
  const iconRailBadge = iconRailBtn?.querySelector('.icon-rail-badge') || null;

  const state = {
    rows: [],            // last loaded rows
    lastModified: null,  // server Last-Modified header
    selected: null,      // thread_id
    filter: 'all',
    firstLoad: true,
    pollTimer: null,
    detailReq: 0,        // monotonic request id for race-safety
    lastSeenLatest: null,
    pendingNew: 0,
    everSucceeded: false, // BUG-2 fix: hide error until first success exists
    failStreak: 0,        // BUG-2 fix: tolerate 1 transient failure
  };

  /* ---- helpers ---- */
  function fmtRel(iso) {
    if (!iso) return '';
    const ts = Date.parse(iso);
    if (!ts) return '';
    const diff = Math.max(0, Date.now() - ts);
    const m = Math.floor(diff / 60000);
    if (m < 1) return 'just now';
    if (m < 60) return m + 'm';
    const h = Math.floor(m / 60);
    if (h < 24) return h + 'h';
    const d = Math.floor(h / 24);
    if (d < 7) return d + 'd';
    return new Date(ts).toLocaleDateString();
  }
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function avatarClassFor(name) {
    if (window.avatarClass) try { return window.avatarClass(name); } catch (_) {}
    // fallback: deterministic 1..6
    let h = 0; for (const c of (name || '?')) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    return 'av-' + ((h % 6) + 1);
  }
  function statusBadge(status) {
    if (status === 'resolved') return '<span class="row-status">✅ resolved</span>';
    if (status === 'idle')     return '<span class="row-status">⏸ idle</span>';
    return '<span class="row-status">💭 in-progress</span>';
  }

  /* ---- rendering ---- */
  function renderList() {
    const rows = state.rows;
    countEl.textContent = rows.length ? `(${rows.length})` : '';
    listEl.innerHTML = '';
    if (!rows.length) {
      emptyEl.hidden = false;
      emptyEl.textContent = state.firstLoad
        ? '还没有任何 thread —— 去某个 squad 开第一个 thread 吧。'
        : '当前 filter 下没有结果。';
      return;
    }
    emptyEl.hidden = true;
    for (const r of rows) {
      const li = document.createElement('li');
      li.className = 'a-row';
      if (r.thread_id === state.selected) li.classList.add('selected');
      // unread inference: v0.1 没有 seen 状态后端，先不画 unread 蓝点（避免假信号）
      li.dataset.threadId = r.thread_id;
      const avName = r.latest_author_human || r.latest_author || r.started_by || '?';
      const avLetter = (avName || '?').slice(0, 1).toUpperCase();
      const avClass = avatarClassFor(avName);
      li.innerHTML = `
        <div class="a-row-avatar ${escapeHtml(avClass)}">${escapeHtml(avLetter)}</div>
        <div class="a-row-body">
          <div class="a-row-top">
            <span class="a-row-title">${escapeHtml(r.title || '(empty)')}</span>
            <span class="a-row-time" title="${escapeHtml(r.latest_post_at || '')}">${escapeHtml(fmtRel(r.latest_post_at))}</span>
          </div>
          <div class="a-row-meta">${escapeHtml(r.squad_name || r.squad_id || '')}</div>
          <div class="a-row-snippet">${escapeHtml(r.latest_snippet || '')}</div>
          <div class="a-row-foot">
            ${statusBadge(r.status)}
            <span class="sep">·</span>
            <span>${r.post_count} posts</span>
            <span class="sep">·</span>
            <span>${r.participant_count} people</span>
            <span class="sep">·</span>
            <span>@${escapeHtml(avName)}</span>
          </div>
        </div>`;
      li.addEventListener('click', () => selectThread(r.thread_id));
      listEl.appendChild(li);
    }
  }

  function setLoading(on) {
    loadingEl.hidden = !on;
    if (on) listEl.style.opacity = '0.4';
    else    listEl.style.opacity = '';
  }

  function setError(msg) {
    if (!msg) { errorBanner.hidden = true; return; }
    errorBanner.hidden = false;
    errorBanner.querySelector('[data-msg]').textContent = '加载失败：' + msg;
  }

  function updateUnreadBadge() {
    // v0.1: 没有 per-user unread 后端字段。产品信号由 designer 锁为
    // 「有任何未解决的 thread 」——这里近似用「any non-closed thread」。
    // icon-rail 总是窄状态，只贴 8px 红点，不显示数字 badge（留给未来 hover/expanded）。
    const hasAny = state.rows.some(r => !r.closed);
    if (iconRailDot) iconRailDot.hidden = !hasAny;
    if (iconRailBadge) iconRailBadge.hidden = true;
  }

  /* ---- fetch ---- */
  async function fetchActivity(useIfMod = true) {
    const headers = {};
    if (useIfMod && state.lastModified) headers['If-Modified-Since'] = state.lastModified;
    let resp;
    try {
      resp = await fetch('/api/activity?filter=' + encodeURIComponent(state.filter), { headers });
    } catch (e) {
      // network error: bump failure streak; only show a banner after 2 in a row,
      // and prefer offline (mutually exclusive with error).
      state.failStreak += 1;
      if (state.failStreak >= 2) {
        // show only one of the two banners. If browser reports offline, prefer offline.
        const isOffline = (typeof navigator !== 'undefined') && navigator.onLine === false;
        if (isOffline) {
          offlineBanner.hidden = false;
          setError(null);
        } else if (state.everSucceeded) {
          // we had data once and now fetch fails: real error
          offlineBanner.hidden = true;
          setError(e.message || String(e));
        } else {
          // never succeeded yet: keep banners hidden, list empty-state will tell the story
          offlineBanner.hidden = true;
          setError(null);
        }
      }
      return null;
    }
    // any HTTP response means we're not offline; clear offline
    offlineBanner.hidden = true;
    if (resp.status === 304) {
      state.failStreak = 0;
      setError(null);
      return { unchanged: true };
    }
    if (!resp.ok) {
      state.failStreak += 1;
      if (state.failStreak >= 2 && state.everSucceeded) setError('HTTP ' + resp.status);
      return null;
    }
    state.failStreak = 0;
    state.everSucceeded = true;
    setError(null);
    const lm = resp.headers.get('Last-Modified');
    if (lm) state.lastModified = lm;
    const data = await resp.json();
    return { rows: data.threads || [] };
  }

  async function loadInitial() {
    setLoading(true);
    const r = await fetchActivity(false);
    setLoading(false);
    if (r && r.rows) {
      state.rows = r.rows;
      state.lastSeenLatest = r.rows[0]?.latest_post_at || null;
      state.firstLoad = false;
      renderList();
      updateUnreadBadge();
    } else if (!r) {
      renderList();
    }
  }

  async function poll() {
    const r = await fetchActivity(true);
    if (!r) return;
    if (r.unchanged) return;
    // detect new activity above current viewport
    const newest = r.rows[0]?.latest_post_at || null;
    const wasAtTop = listEl.scrollTop <= 8;
    if (newest && newest !== state.lastSeenLatest && !wasAtTop) {
      // count rows ahead of last seen
      let count = 0;
      for (const row of r.rows) {
        if (row.latest_post_at === state.lastSeenLatest) break;
        count++;
      }
      state.pendingNew = count || 1;
      newChip.hidden = false;
      newChip.querySelector('[data-count]').textContent = state.pendingNew;
    }
    state.rows = r.rows;
    if (wasAtTop) state.lastSeenLatest = newest;
    renderList();
    updateUnreadBadge();
  }

  function schedulePoll() {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(async () => {
      try { await poll(); } catch (_) {}
      if (!view.hidden) schedulePoll();
    }, POLL_MS);
  }

  /* ---- detail (right pane: reuses #thread-pane in full) ---- */
  // Track relocated #thread-pane + its origin so we can put it back on exit.
  let _threadPaneOrigParent = null;
  let _threadPaneOrigNext = null;
  function mountThreadPane() {
    const pane = document.getElementById('thread-pane');
    if (!pane || !paneMount) return;
    if (pane.parentNode === paneMount) return; // already mounted
    _threadPaneOrigParent = pane.parentNode;
    _threadPaneOrigNext = pane.nextSibling;
    paneMount.appendChild(pane);
  }
  function unmountThreadPane() {
    const pane = document.getElementById('thread-pane');
    if (!pane || !_threadPaneOrigParent) return;
    if (pane.parentNode === paneMount) {
      if (_threadPaneOrigNext && _threadPaneOrigNext.parentNode === _threadPaneOrigParent) {
        _threadPaneOrigParent.insertBefore(pane, _threadPaneOrigNext);
      } else {
        _threadPaneOrigParent.appendChild(pane);
      }
    }
    _threadPaneOrigParent = null;
    _threadPaneOrigNext = null;
  }

  async function selectThread(tid) {
    state.selected = tid;
    renderList(); // updates selected class
    // Hide empty placeholder; let the real thread pane take over.
    if (detailEmpty) detailEmpty.hidden = true;
    if (detailLoading) detailLoading.hidden = true;
    if (detailError) detailError.hidden = true;
    location.hash = '#/activity/' + tid;
    // Soft-sync squad so that switching to Threads view shows this thread
    // selected in its middle list too (no cross-view orphan-detail split).
    // Safety per alice: only switch squad if it's in the user's visible list;
    // otherwise just update the thread id and accept the small split rather
    // than dropping the user into a squad they can't see.
    try {
      const row = state.rows.find(r => r.thread_id === tid);
      const sid = row && row.squad_id;
      if (sid && window.state && window.state.squads &&
          window.state.squads.some(s => s.id === sid)) {
        if (window.state.currentSquadId !== sid) {
          window.state.currentSquadId = sid;
          if (window.refreshThreadsForCurrentSquad) {
            window.refreshThreadsForCurrentSquad();
          }
          if (window.renderSquadRail) window.renderSquadRail();
        }
      }
    } catch (_) {}
    if (window.selectThread && window.selectThread !== selectThread) {
      // Use the home-view's full selectThread (loads detail + opens SSE + wires composer).
      try { await window.selectThread(tid); } catch (_) {}
    }
  }

  function clearDetail() {
    state.selected = null;
    if (detailEmpty) detailEmpty.hidden = false;
  }

  /* ---- chips ---- */
  chips.forEach(ch => {
    ch.addEventListener('click', () => {
      if (ch.disabled) return;
      chips.forEach(c => c.classList.toggle('is-active', c === ch));
      state.filter = ch.dataset.filter;
      state.firstLoad = true;
      state.lastModified = null;
      loadInitial();
    });
  });

  /* ---- new-chip click ---- */
  newChip.addEventListener('click', () => {
    listEl.scrollTo({ top: 0, behavior: 'smooth' });
    state.lastSeenLatest = state.rows[0]?.latest_post_at || null;
    state.pendingNew = 0;
    newChip.hidden = true;
  });
  listEl.addEventListener('scroll', () => {
    if (listEl.scrollTop <= 8 && state.pendingNew) {
      state.lastSeenLatest = state.rows[0]?.latest_post_at || null;
      state.pendingNew = 0;
      newChip.hidden = true;
    }
  });

  errorBanner.querySelector('[data-retry]').addEventListener('click', () => {
    setError(null);
    loadInitial();
  });

  /* ---- entry/exit ---- */
  window.__forgeActivityActivate = function () {
    // BUG-2 fix: clear stale banners on entry; let the next fetch decide.
    setError(null);
    offlineBanner.hidden = true;
    state.failStreak = 0;
    mountThreadPane();
    // If nothing selected, show the empty placeholder; thread-pane shows itself only when selectThread is called.
    if (!state.selected) { if (detailEmpty) detailEmpty.hidden = false; }
    if (state.rows.length === 0) {
      loadInitial().then(() => schedulePoll());
    } else {
      renderList();
      updateUnreadBadge();
      schedulePoll();
    }
  };
  window.__forgeActivityDeactivate = function () {
    if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
    unmountThreadPane();
  };
  window.__forgeActivitySelect = function (tid) { selectThread(tid); };

  // Hook for the home-view's submitPost: after a post lands, refresh the
  // activity rows so the row jumps to the top + snippet updates immediately
  // (alice's edge case #3: optimistic feedback, don't wait for next poll).
  window.__forgeActivityNudge = function () {
    if (view.hidden) return;
    state.lastModified = null; // bypass 304 so we get fresh data + ordering
    poll();
  };

  // Background heartbeat for the icon-rail red dot (every 60s when not in Activity)
  setInterval(async () => {
    if (!view.hidden) return; // foreground poll handles it
    try {
      const r = await fetchActivity(true);
      if (r && r.rows) { state.rows = r.rows; updateUnreadBadge(); }
    } catch (_) {}
  }, 60000);
})();

/* ─── PRD v1.1 Favorites ─────────────────────────────────────────────── */
/* State machine (designer §4, 11 states):
   idle / loading / ready{empty,filled,partial-missing} /
   error / retry / optimistic / confirmed / rollback / filtering / empty-result.
   We track a single `view` string + a `searching` boolean; chip-side
   optimistic updates round-trip through the same set so the chip ★ and
   the list stay in sync without SSE (PRD §3 跨 tab is V2). */
(function () {
  window._forgeFavSet = window._forgeFavSet || new Set(); // abs_path strings
  window._forgeFavLoaded = false;

  const root = document.getElementById('favorites-list');
  if (!root) return;

  const els = {
    list: document.getElementById('favorites-list'),
    search: document.getElementById('favorites-search'),
    empty: document.getElementById('favorites-empty'),
    emptySearch: document.getElementById('favorites-empty-search'),
    error: document.getElementById('favorites-error'),
    loading: document.getElementById('favorites-loading'),
    retry: document.getElementById('favorites-retry'),
    refresh: document.getElementById('btn-favorites-refresh'),  // null in v2
    total: document.getElementById('favorites-total'),         // null in v2
    tabCount: document.getElementById('files-tab-favorites-count'),
    status: document.getElementById('favorites-status'),
  };

  let state = {
    items: [],          // full list from server
    filtered: [],
    view: 'idle',
    searching: false,
    error: null,
  };

  function setView(v) { state.view = v; render(); }

  function render() {
    const hide = el => { if (el) el.hidden = true; };
    [els.list, els.empty, els.emptySearch, els.error, els.loading].forEach(hide);
    if (els.status) els.status.hidden = true;

    if (state.view === 'loading' || state.view === 'idle') {
      els.loading.hidden = false;
      return;
    }
    if (state.view === 'error' || state.view === 'retry') {
      els.error.hidden = false;
      return;
    }
    // ready
    const q = (els.search.value || '').trim().toLowerCase();
    state.filtered = q
      ? state.items.filter(it =>
          (it.label || '').toLowerCase().includes(q) ||
          (it.preview || '').toLowerCase().includes(q))
      : state.items.slice();

    if (state.filtered.length === 0) {
      (q ? els.emptySearch : els.empty).hidden = false;
      return;
    }
    els.list.hidden = false;
    const someMissing = state.items.some(it => it.missing_state === 'missing');
    const someUnknown = state.items.some(it => it.missing_state === 'unknown');
    if ((someMissing || someUnknown) && els.status) {
      const parts = [];
      if (someMissing) parts.push('部分文件已不在原位置');
      if (someUnknown) parts.push('部分文件状态未知（可能在 sleep 中的外置盘）');
      els.status.textContent = parts.join(' · ');
      els.status.hidden = false;
    }
    els.list.innerHTML = state.filtered.map(renderCard).join('');
  }

  function formatTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    if (sameDay) return `今天 ${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}`;
    return d.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function threadShort(tid) {
    if (!tid) return '—';
    // th_19e6f1781c0_77d68a → th_…68a
    const tail = tid.slice(-6);
    return `th_…${tail}`;
  }

  function renderCard(it) {
    const isMissing = it.missing_state === 'missing';
    const isUnknown = it.missing_state === 'unknown';
    const cls = 'favorite-card'
      + (isMissing ? ' is-missing' : '')
      + (isUnknown ? ' is-unknown' : '');
    const icon = isMissing ? '⚠️' : '📄';
    const agent = it.source_agent || '—';
    const thread = it.source_thread_id ? threadShort(it.source_thread_id) : '—';
    const time = formatTime(it.favorited_at);
    const preview = isMissing
      ? '<strong>文件已不在原位置</strong>'
      : (isUnknown
          ? '<em>状态未知</em>'
          : escapeHtml(it.preview || '(无预览)'));
    const absAttr = escapeAttr(it.abs_path);
    const labelEsc = escapeHtml(it.label || it.abs_path);
    return `
      <li class="${cls}" data-abs="${absAttr}">
        <div class="favorite-card-head">
          <span class="favorite-card-icon">${icon}</span>
          <button type="button" class="favorite-card-title" data-fav-open="1">${labelEsc}</button>
          <div class="favorite-card-actions">
            <button type="button" class="fav-star" data-fav-unstar="1" title="取消收藏" aria-label="取消收藏 ${escapeAttr(it.label)}">★</button>
            <button type="button" data-fav-copy="1" title="复制路径" aria-label="复制路径">⧉</button>
          </div>
        </div>
        <p class="favorite-card-preview">${preview}</p>
        <div class="favorite-card-meta">
          <span>${escapeHtml(agent)}</span>
          <span class="dot-sep">·</span>
          <span>${escapeHtml(thread)}</span>
          <span class="dot-sep">·</span>
          <span>收藏于 ${escapeHtml(time)}</span>
        </div>
      </li>`;
  }

  async function load() {
    setView('loading');
    try {
      const r = await fetch('/api/favorites');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      const items = Array.isArray(data.favorites) ? data.favorites : [];
      state.items = items;
      window._forgeFavSet = new Set(items.map(x => x.abs_path));
      window._forgeFavLoaded = true;
      if (els.total) els.total.textContent = `(${items.length})`;
      updateRailBadge(items.length);
      setView('ready');
    } catch (e) {
      state.error = e.message || String(e);
      setView('error');
    }
  }

  function updateRailBadge(n) {
    // v2: ⭐ rail entry removed; show count next to the Favorites tab label.
    if (!els.tabCount) return;
    if (n > 0) {
      els.tabCount.hidden = false;
      els.tabCount.textContent = n > 99 ? '99+' : String(n);
    } else {
      els.tabCount.hidden = true;
    }
  }

  els.refresh && (els.refresh.onclick = load);
  els.retry && (els.retry.onclick = load);
  els.search && els.search.addEventListener('input', () => {
    clearTimeout(els.search._t);
    els.search._t = setTimeout(render, 300);
  });

  // Card actions delegation
  els.list && els.list.addEventListener('click', async (e) => {
    const li = e.target.closest('.favorite-card');
    if (!li) return;
    const abs = li.dataset.abs;
    if (e.target.matches('[data-fav-unstar]')) {
      if (li.classList.contains('is-missing')) {
        if (!confirm('文件已不存在，是否从收藏移除？')) return;
      }
      try {
        await toggleFavorite(abs, false, {});
        window._forgeFavSet.delete(abs);
        // PRD v1.3 AC-14 reverse direction (judy review): unstar from
        // Favorites tab must also flip viewer ★ + chip ★ in lock-step.
        // syncAllStarsAfterToggle handles list filter + badge + render +
        // chip refresh + viewer refresh in one go.
        syncAllStarsAfterToggle(abs, false);
      } catch (_) { showToast('取消收藏失败，已撤销'); }
      return;
    }
    if (e.target.matches('[data-fav-copy]')) {
      try { await navigator.clipboard.writeText(abs); showToast('路径已复制'); } catch { showToast('复制失败'); }
      return;
    }
    if (e.target.matches('[data-fav-open]')) {
      // designer §3.2: missing 卡 title click 不进 viewer，弹 confirm 让 scott 决定
      if (li.classList.contains('is-missing')) {
        if (confirm('文件已不存在，是否从收藏移除？')) {
          try {
            await toggleFavorite(abs, false, {});
            window._forgeFavSet.delete(abs);
            // AC-14 reverse: keep three views in sync.
            syncAllStarsAfterToggle(abs, false);
          } catch (_) { showToast('移除失败，已撤销'); }
        }
        return;
      }
      // Try to navigate to the matching ref if known.
      const idx = window._forgeRefs;
      if (idx && idx.all) {
        const hit = idx.all.find(r => r.abs_path === abs);
        if (hit) { location.hash = `#/files/refs/${encodeURIComponent(hit.id)}`; return; }
      }
      showToast('未找到对应 ref（可能已注销）');
    }
  });

  // chip ⭐ delegation — global, fires for any .file-chip-fav anywhere in
  // the document (thread pane, file viewer, activity, …).
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('.file-chip-fav');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const abs = btn.dataset.favAbs;
    if (!abs) return;
    const wasFav = btn.classList.contains('is-favorited');
    // optimistic flip
    setStarUI(btn, !wasFav);
    btn.classList.add('is-flipping');
    setTimeout(() => btn.classList.remove('is-flipping'), 200);
    try {
      await toggleFavorite(abs, !wasFav, {
        ref_id: btn.dataset.favRef || null,
        source_agent: btn.dataset.favAgent || null,
        thread_id: btn.dataset.favThread || null,
      });
      if (!wasFav) window._forgeFavSet.add(abs);
      else window._forgeFavSet.delete(abs);
      // PRD v1.2 viewer entry: same-tab sync — keep viewer ★ + Favorites
      // tab in lock-step with any chip toggle.
      syncAllStarsAfterToggle(abs, !wasFav);
    } catch (err) {
      // rollback
      setStarUI(btn, wasFav);
      showToast(wasFav ? '取消收藏失败，已撤销' : '收藏失败，已撤销');
    }
  });

  function setStarUI(btn, fav) {
    btn.classList.toggle('is-favorited', !!fav);
    btn.textContent = fav ? '★' : '☆';
    btn.setAttribute('aria-pressed', fav ? 'true' : 'false');
  }

  function refreshChipStars() {
    document.querySelectorAll('.file-chip-fav').forEach(btn => {
      const abs = btn.dataset.favAbs;
      if (!abs) return;
      setStarUI(btn, window._forgeFavSet.has(abs));
    });
  }

  /* ---- viewer ⭐ button (PRD v1.2, alice 20:32 / designer 20:38) ---- */
  const viewerFavBtn = document.getElementById('btn-viewer-fav');
  let viewerFavCtx = null;  // { abs_path, ref_id, source_agent, thread_id }

  function setViewerFavUI(fav, busy) {
    if (!viewerFavBtn) return;
    viewerFavBtn.classList.toggle('is-favorited', !!fav);
    viewerFavBtn.classList.toggle('is-busy', !!busy);
    viewerFavBtn.textContent = fav ? '★' : '☆';
    viewerFavBtn.setAttribute('aria-pressed', fav ? 'true' : 'false');
    viewerFavBtn.setAttribute('aria-label', fav ? '取消收藏' : '收藏此文件');
    viewerFavBtn.title = fav ? '取消收藏' : '收藏';
    if (busy) viewerFavBtn.setAttribute('aria-busy', 'true');
    else viewerFavBtn.removeAttribute('aria-busy');
    viewerFavBtn.disabled = !!busy;
  }

  // Exposed to selectRef / clearSelection (above). target=null = hide.
  window.setViewerFavTarget = function (target) {
    if (!viewerFavBtn) return;
    if (!target || !target.abs_path) {
      viewerFavCtx = null;
      viewerFavBtn.hidden = true;
      return;
    }
    viewerFavCtx = target;
    viewerFavBtn.hidden = false;
    setViewerFavUI(window._forgeFavSet.has(target.abs_path), false);
  };

  function refreshViewerStar() {
    if (!viewerFavBtn || !viewerFavCtx) return;
    setViewerFavUI(window._forgeFavSet.has(viewerFavCtx.abs_path), false);
  }

  // Side-effect: same-tab sync. After any successful toggle, refresh chip
  // stars + viewer button + Favorites tab list if it's currently rendered.
  function syncAllStarsAfterToggle(abs, nowFav) {
    refreshChipStars();
    refreshViewerStar();
    // Favorites tab in-memory state
    if (nowFav) {
      // Server is authoritative for the full row (preview, meta) — a stale
      // reload happens next time the user opens the tab. Best effort: if we
      // don't have it, leave it; if we do, keep it.
    } else {
      state.items = state.items.filter(x => x.abs_path !== abs);
      if (els.total) els.total.textContent = `(${state.items.length})`;
      updateRailBadge(state.items.length);
      if (state.view === 'ready') render();
    }
  }

  if (viewerFavBtn) {
    viewerFavBtn.addEventListener('click', async () => {
      if (!viewerFavCtx || !viewerFavCtx.abs_path) return;
      const abs = viewerFavCtx.abs_path;
      const wasFav = window._forgeFavSet.has(abs);
      // optimistic
      setViewerFavUI(!wasFav, true);
      try {
        await toggleFavorite(abs, !wasFav, {
          ref_id: viewerFavCtx.ref_id || null,
          source_agent: viewerFavCtx.source_agent || null,
          thread_id: viewerFavCtx.thread_id || null,
        });
        if (!wasFav) window._forgeFavSet.add(abs);
        else window._forgeFavSet.delete(abs);
        setViewerFavUI(!wasFav, false);
        syncAllStarsAfterToggle(abs, !wasFav);
      } catch (_) {
        // rollback
        setViewerFavUI(wasFav, false);
        showToast(wasFav ? '取消收藏失败，已撤销' : '收藏失败，已撤销');
      }
    });
  }

  async function toggleFavorite(abs_path, favorited, extra) {
    const body = { abs_path, favorited, ...extra };
    const r = await fetch('/api/favorites', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  // Public: activate (called when entering the view)
  window.__forgeFavoritesActivate = function () { load(); };

  // Public: bootstrap favorites cache so chip ★ states render on first
  // load too. Fire and forget; chip renderer is safe with empty set.
  (async function bootstrap() {
    try {
      const r = await fetch('/api/favorites');
      if (!r.ok) return;
      const data = await r.json();
      const items = Array.isArray(data.favorites) ? data.favorites : [];
      window._forgeFavSet = new Set(items.map(x => x.abs_path));
      window._forgeFavLoaded = true;
      updateRailBadge(items.length);
      if (els.total) els.total.textContent = `(${items.length})`;
    } catch (_) { /* swallow */ }
  })();
})();

/* ─── Theme toggle (designer/openforge-theme-tokens-v0.1 §3) ──────
   Three-state: light | dark | system. Bootstrap (FOUC-safe) lives
   inline in index.html <head>; this only wires the toolbar buttons
   and persists user choice. */
(function () {
  const KEY = 'openforge.theme';
  const root = document.documentElement;

  function currentChoice() {
    try { return localStorage.getItem(KEY) || 'system'; } catch (_) { return 'system'; }
  }
  function applyChoice(choice) {
    if (choice === 'light' || choice === 'dark') {
      root.setAttribute('data-theme', choice);
    } else {
      root.removeAttribute('data-theme');
    }
    try {
      if (choice === 'system') localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, choice);
    } catch (_) {}
    syncButtons(choice);
  }
  function syncButtons(choice) {
    const btns = document.querySelectorAll('#theme-toggle .theme-toggle-btn');
    btns.forEach((b) => {
      b.classList.toggle('is-active', b.dataset.themeChoice === choice);
      b.setAttribute('aria-pressed', String(b.dataset.themeChoice === choice));
    });
  }
  function init() {
    const wrap = document.getElementById('theme-toggle');
    if (!wrap) return;
    wrap.addEventListener('click', (e) => {
      const btn = e.target.closest('.theme-toggle-btn');
      if (!btn) return;
      const choice = btn.dataset.themeChoice;
      if (choice !== 'light' && choice !== 'dark' && choice !== 'system') return;
      applyChoice(choice);
      // Always-on click feedback, decoupled from whether the theme actually flipped
      // (e.g. clicking 🌙 while system is already dark would otherwise feel like a no-op).
      btn.classList.remove('just-clicked');
      // Force reflow so the animation restarts on rapid repeat clicks.
      // eslint-disable-next-line no-unused-expressions
      btn.offsetWidth;
      btn.classList.add('just-clicked');
      setTimeout(() => btn.classList.remove('just-clicked'), 360);
    });
    syncButtons(currentChoice());
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();

/* ─── squad-rail collapsible (feat/squad-rail-collapsible) ─────────────
 *
 * Two independent inputs, one derived state:
 *   userPreference: 'collapsed' | 'expanded' | null   (localStorage)
 *   autoCollapsed:  boolean                            (matchMedia < 1100px)
 *   effective = userPreference ?? (autoCollapsed ? 'collapsed' : 'expanded')
 *
 * Once the user clicks the toggle, userPreference wins forever (no narrow→
 * wide→narrow re-auto-collapse). bobby's call per thread th_19eb...; if dora
 * wants re-arm semantics we'll add a reset entry-point later.
 */
(function squadRailCollapsible() {
  const LS_KEY = 'openforge.sidebar.squad.collapsed';
  const BREAKPOINT = '(max-width: 1099px)';

  const body = document.body;
  const toggleBtn = document.getElementById('btn-toggle-squad-collapse');
  const rail = document.getElementById('squad-rail');
  if (!toggleBtn || !rail) return;

  // ── state ──────────────────────────────────────────────────────────
  let userPreference = null;  // 'collapsed' | 'expanded' | null
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw === 'true' || raw === '"collapsed"' || raw === 'collapsed') userPreference = 'collapsed';
    else if (raw === 'false' || raw === '"expanded"' || raw === 'expanded') userPreference = 'expanded';
  } catch { /* ignore */ }

  const mql = window.matchMedia(BREAKPOINT);
  let autoCollapsed = mql.matches;

  function effective() {
    if (userPreference !== null) return userPreference === 'collapsed';
    return autoCollapsed;
  }

  function apply() {
    const collapsed = effective();
    body.classList.toggle('squad-collapsed', collapsed);
    toggleBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    toggleBtn.setAttribute('aria-label', collapsed ? '展开 Squad 栏' : '折叠 Squad 栏');
    toggleBtn.setAttribute('title', collapsed ? '展开 Squad 栏 (⌘\\)' : '折叠 Squad 栏 (⌘\\)');
    toggleBtn.textContent = collapsed ? '»' : '«';
    if (collapsed) hideTooltip();
  }

  function setUserPreference(next) {
    userPreference = next;
    try { localStorage.setItem(LS_KEY, next); } catch { /* ignore */ }
    apply();
  }

  // ── toggle handler ─────────────────────────────────────────────────
  toggleBtn.addEventListener('click', () => {
    setUserPreference(effective() ? 'expanded' : 'collapsed');
  });

  // ── breakpoint listener ────────────────────────────────────────────
  const onMqlChange = (e) => {
    autoCollapsed = e.matches;
    apply();
  };
  if (mql.addEventListener) mql.addEventListener('change', onMqlChange);
  else if (mql.addListener) mql.addListener(onMqlChange);

  // ── keyboard: Cmd/Ctrl+\ ───────────────────────────────────────────
  // (#N is taken by new-thread; Backslash is unused elsewhere — confirmed
  //  by grep across web/app.js.) Use code='Backslash' to be layout-safe.
  window.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey
        && (e.code === 'Backslash' || e.key === '\\')) {
      e.preventDefault();
      setUserPreference(effective() ? 'expanded' : 'collapsed');
    }
  });

  // ── tooltip for collapsed-mode icons ───────────────────────────────
  let tooltipEl = null;
  let hoverTimer = null;
  let pressTimer = null;

  function ensureTooltip() {
    if (tooltipEl) return tooltipEl;
    tooltipEl = document.createElement('div');
    tooltipEl.id = 'squad-rail-tooltip';
    tooltipEl.setAttribute('role', 'tooltip');
    document.body.appendChild(tooltipEl);
    return tooltipEl;
  }
  function showTooltip(targetEl, text) {
    if (!text) return;
    const el = ensureTooltip();
    el.textContent = text;
    const r = targetEl.getBoundingClientRect();
    el.style.left = (r.right + 8) + 'px';
    el.style.top  = Math.round(r.top + (r.height / 2) - (el.offsetHeight / 2 || 12)) + 'px';
    el.classList.add('visible');
    // re-anchor now that we know real height
    requestAnimationFrame(() => {
      el.style.top = Math.round(r.top + (r.height / 2) - (el.offsetHeight / 2)) + 'px';
    });
  }
  function hideTooltip() {
    if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
    if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
    if (tooltipEl) tooltipEl.classList.remove('visible');
  }

  function collapsedSquadItem(target) {
    if (!body.classList.contains('squad-collapsed')) return null;
    const item = target.closest && target.closest('.squad-item');
    if (!item || !rail.contains(item)) return null;
    return item;
  }
  function squadLabelFor(item) {
    const nameEl = item.querySelector('.squad-name');
    if (!nameEl) return '';
    // Skip the inline '<span class="archived-tag">archived</span>' tail
    return (nameEl.firstChild && nameEl.firstChild.nodeType === Node.TEXT_NODE
              ? nameEl.firstChild.textContent
              : nameEl.textContent || '').trim();
  }

  // Mouse hover (400ms delay so quick scans don't pop tooltips)
  rail.addEventListener('mouseover', (e) => {
    const item = collapsedSquadItem(e.target);
    if (!item) return;
    if (hoverTimer) clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => showTooltip(item, squadLabelFor(item)), 400);
  });
  rail.addEventListener('mouseout', (e) => {
    if (collapsedSquadItem(e.target)) hideTooltip();
  });
  // Hide if rail scrolls or window resizes — avoid stale anchor.
  rail.addEventListener('scroll', hideTooltip, true);
  window.addEventListener('resize', hideTooltip);
  window.addEventListener('blur', hideTooltip);

  // Touch: long-press 300ms triggers tooltip
  rail.addEventListener('touchstart', (e) => {
    const item = collapsedSquadItem(e.target);
    if (!item) return;
    if (pressTimer) clearTimeout(pressTimer);
    pressTimer = setTimeout(() => showTooltip(item, squadLabelFor(item)), 300);
  }, { passive: true });
  rail.addEventListener('touchend', hideTooltip);
  rail.addEventListener('touchcancel', hideTooltip);

  // Accessibility: when collapsed, give the squad button an aria-label
  // (VoiceOver) — patch on render via a MutationObserver since renderSquadRail
  // wipes/rebuilds the list.
  function annotateAriaLabels() {
    rail.querySelectorAll('.squad-item').forEach((btn) => {
      const label = squadLabelFor(btn);
      if (label) btn.setAttribute('aria-label', label);
    });
  }
  const obs = new MutationObserver(annotateAriaLabels);
  const listEl = document.getElementById('squad-list');
  if (listEl) obs.observe(listEl, { childList: true, subtree: true });
  annotateAriaLabels();

  apply();
})();

/* ─── collapsed-mode settings entry (footer ⚙) ─── */
(function squadRailCollapsedSettings() {
  const collapsedBtn = document.getElementById('btn-settings-collapsed');
  const realBtn = document.getElementById('btn-settings');
  if (!collapsedBtn || !realBtn) return;
  collapsedBtn.addEventListener('click', () => realBtn.click());
})();
