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
  const cls = `avatar ${avatarClass(name)}${extraClass ? ' ' + extraClass : ''}`;
  const label = escapeHtml(avatarLabel(name));
  const friendly = displayName(name);
  if (isEmployee(name)) {
    const href = webchatLinkFor(name, threadId);
    const title = threadId
      ? `点击查看 ${friendly} 在本 thread 的 session`
      : `点击查看 ${friendly} 的 main session`;
    return `<a class="${cls} avatar-link" href="${href}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(title)}"${styleAttr}>${label}</a>`;
  }
  return `<div class="${cls}"${styleAttr} title="${escapeHtml(friendly || '')}">${label}</div>`;
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
      // unresolved + not even a v0.7-shaped path → render as plain text
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
      return `<a class="file-chip file-chip-ref" href="${href}" title="${escapeAttr(c.ref.label + ' · ' + (c.ref.source_agent || ''))}" data-file-chip="1" data-ref-id="${escapeAttr(c.ref.id)}">`
        + `<span class="file-chip-icon">📄</span>${escapeHtml(c.display)}${agentTag}</a>`;
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
    if (!state.currentSquadId && state.squads.length) {
      state.currentSquadId = state.squads[0].id;
    }
    renderSquadRail();
    renderThreadRail();
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
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'squad-item'
      + (squad.id === state.currentSquadId ? ' active' : '')
      + (squad.archived ? ' archived' : '');
    btn.innerHTML = `
      <span class="squad-emoji">${escapeHtml(squad.emoji || '#')}</span>
      <span class="squad-name">${escapeHtml(squad.name || squad.id)}${squad.archived ? ' <span class="archived-tag">archived</span>' : ''}</span>
      <span class="squad-count">${count}</span>
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
  } catch (err) {
    setStatus(`thread 列表加载失败: ${err.message}`, false);
  }
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
    li.className = 'thread-item' + closedCls
      + (t.thread_id === state.currentThreadId ? ' active' : '');
    const liveDot = t.in_progress ? '<span class="live-dot"></span>' : '';
    const closedChip = t.in_progress
      ? ''
      : '<span class="thread-closed-chip" title="Closed">🔒</span>';
    li.innerHTML = `
      <button type="button">
        <div class="thread-line-1">
          ${liveDot}
          <span class="thread-preview">${escapeHtml(t.title || t.preview || '(empty)')}</span>
          ${closedChip}
        </div>
        <div class="thread-line-2">
          <span class="thread-by">${escapeHtml(t.created_by)}</span>
          <span class="dot-sep">·</span>
          <span>${t.post_count} ${t.post_count === 1 ? 'post' : 'posts'}</span>
          <span class="dot-sep">·</span>
          <span class="thread-time">${escapeHtml(formatRelative(t.last_post_at))}</span>
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
    renderDetail();
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
    renderDetail({ keepScroll: true });
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
    const cls = `mini-avatar ${avatarClass(name)}`;
    const label = avatarLabel(name);
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
    el.textContent = label;
    els.detailParticipants.appendChild(el);
  });
}

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
  // V1.1: flat chronological list. Reply context is rendered as an inline
  // quote card at the top of each child post (see renderPostNode), not as
  // tree nesting — that was too visually heavy in real threads.
  live.forEach(p => els.postList.appendChild(renderPostNode(p, false)));
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
        <span class="post-time" title="${escapeHtml(post.ts || '')}">${escapeHtml(post.time || '')}</span>
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
// only agents with ~/.openclaw/workspace-<id>/SOUL.md on the server side,
// so adding a new employee requires zero front-end code changes.
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
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
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
    if (state.currentSquadId) refreshThreadsForCurrentSquad();
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
  loadSquads().then(() => { refreshAgentList(); startPolling(); });
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
    const hideAll = () => {
      homeView.hidden = true;
      filesView.hidden = true;
      if (agentsView) agentsView.hidden = true;
    };
    if (view === 'files') {
      hideAll();
      filesView.hidden = false;
      if (state.activeTab === 'workspace') loadFileList();
    } else if (view === 'agents') {
      hideAll();
      if (agentsView) agentsView.hidden = false;
    } else {
      hideAll();
      homeView.hidden = false;
    }
  }

  function routeFromHash() {
    const h = location.hash || '';
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
      // New: #/files/<root>/<name>
      m = h.match(/^#\/files\/([A-Za-z0-9_\-]{1,32})\/([A-Za-z0-9_.\-]+\.md)$/);
      if (m) {
        switchTab('workspace');
        selectFile(decodeURIComponent(m[2]), decodeURIComponent(m[1]));
        return;
      }
      // Legacy: #/files/<name> — fall back to first known root or default
      m = h.match(/^#\/files\/([A-Za-z0-9_.\-]+\.md)$/);
      if (m) {
        switchTab('workspace');
        selectFile(decodeURIComponent(m[1]), null);
        return;
      }
      // Bare /#/files → default to refs tab
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
  const btnToggle = document.getElementById('btn-toggle-edit');
  const btnSave = document.getElementById('btn-save-file');
  const btnNew = document.getElementById('btn-new-file');
  const rootSelect = document.getElementById('file-root-select');
  // v0.8
  const tabRefs = document.getElementById('files-tab-refs');
  const tabWorkspace = document.getElementById('files-tab-workspace');
  const refsPane = document.getElementById('files-refs-pane');
  const wsPane = document.getElementById('files-workspace-pane');
  const refsListEl = document.getElementById('refs-list');
  const refsEmptyEl = document.getElementById('refs-empty');
  const refsSearchEl = document.getElementById('refs-search');

  function switchTab(tab) {
    state.activeTab = tab;
    const isRefs = tab === 'refs';
    if (tabRefs) tabRefs.classList.toggle('is-active', isRefs);
    if (tabWorkspace) tabWorkspace.classList.toggle('is-active', !isRefs);
    if (refsPane) refsPane.hidden = !isRefs;
    if (wsPane) wsPane.hidden = isRefs;
    if (btnNew) btnNew.style.visibility = isRefs ? 'hidden' : '';
    if (isRefs) {
      loadRefs();
    } else {
      loadFileList();
    }
  }

  if (tabRefs) tabRefs.addEventListener('click', () => { location.hash = '#/files/refs'; });
  if (tabWorkspace) tabWorkspace.addEventListener('click', () => {
    const root = state.currentRoot || (state.roots[0]?.id) || 'files';
    location.hash = '#/files/' + encodeURIComponent(root);
  });
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
      titleEl.textContent = ref.label + (ref.writable ? '' : '  🔒');
      subEl.textContent = (ref.source_agent ? ref.source_agent + ' · ' : '')
        + fmtSize(ref.size_hint || 0) + ' · 注册于 ' + fmtTime(ref.registered_at)
        + ' · ' + ref.abs_path;
      previewEl.innerHTML = '<p class="meta">加载中…</p>';
      editorEl.hidden = true;
      previewEl.hidden = false;
      btnSave.hidden = true;
      btnToggle.disabled = true;
      btnToggle.textContent = '编辑';
      const r = await fetch('/api/refs/' + encodeURIComponent(refId) + '/content');
      if (!r.ok) {
        previewEl.innerHTML = '<p class="meta">加载失败: HTTP ' + r.status + '</p>';
        renderRefsList();
        return;
      }
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
        if (ref.writable) {
          btnToggle.disabled = false;
          btnToggle.title = '';
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
    } else {
      previewEl.textContent = state.content || '';
    }
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
  }

  btnToggle.addEventListener('click', () => {
    if (!state.current) return;
    setMode(state.mode === 'preview' ? 'edit' : 'preview');
  });

  editorEl.addEventListener('input', () => {
    state.content = editorEl.value;
    state.dirty = true;
    btnSave.disabled = false;
    renderPreview();
  });

  async function saveCurrent() {
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
      e.preventDefault();
      saveCurrent();
    }
  });

  window.addEventListener('beforeunload', (e) => {
    if (state.dirty) { e.preventDefault(); e.returnValue = ''; }
  });

  btnNew.addEventListener('click', async () => {
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
  loadRoots().then(() => loadRefIndex()).then(routeFromHash);
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
      li.textContent = '🧑 ' + id;
      li.addEventListener('click', () => { location.hash = '#/agents/' + encodeURIComponent(id); });
      list.appendChild(li);
    }
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
    if (!current) return;
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

  input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const v = normalizeId(input.value);
      if (v) location.hash = '#/agents/' + encodeURIComponent(v);
    }
  });
  refreshBtn?.addEventListener('click', () => { if (current) refreshBundle(current, true); });
  agentsRefreshBtn?.addEventListener('click', discoverAgents);

  window.__forgeAgentsSelect = selectAgent;

  // initial discovery
  discoverAgents();
})();
