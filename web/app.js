// OpenForge web app — squad / thread / posts, vanilla JS, Slack-shaped.

const AGENTS = ['milk', 'sentry', 'bugfix', 'milly', 'kb'];
const POLL_MS = 8000;

const els = {
  // squad rail
  squadList: document.getElementById('squad-list'),
  btnNewSquad: document.getElementById('btn-new-squad'),
  statusDot: document.getElementById('status-dot'),
  statusText: document.getElementById('status-text'),

  // thread rail
  squadTitle: document.getElementById('squad-title'),
  squadDescription: document.getElementById('squad-description'),
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
};

const state = {
  squads: [],
  squadDetails: new Map(),  // squad_id -> { squad, threads }
  currentSquadId: null,
  currentThreadId: null,
  currentThread: null,
  pollTimer: null,
  threadEventSource: null,  // EventSource for the currently-open thread
  threadEventThreadId: null,
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
  return [...(name || '?')][0].toUpperCase();
}

function avatarClass(name) {
  return AGENT_COLOR_CLASS.get(name) || 'av-default';
}

function renderBody(text) {
  let html = escapeHtml(text);
  html = html.replace(MENTION_RE,
    (_, name) => `<span class="mention">@${escapeHtml(name)}</span>`);
  html = html.replace(/`([^`\n]+)`/g,
    (_, code) => `<code>${escapeHtml(code)}</code>`);
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
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ─── squads ───────────────────────────────────────────────────────────
async function loadSquads() {
  setStatus('加载 squads...');
  try {
    state.squads = await apiJson('/api/squads');
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
    btn.className = 'squad-item' + (squad.id === state.currentSquadId ? ' active' : '');
    btn.innerHTML = `
      <span class="squad-emoji">${escapeHtml(squad.emoji || '#')}</span>
      <span class="squad-name">${escapeHtml(squad.name || squad.id)}</span>
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
  renderThreadList(detail?.threads || []);
}

function renderThreadList(threads) {
  els.threadList.innerHTML = '';
  if (!threads.length) {
    els.threadList.innerHTML =
      '<li class="empty-row">还没有 thread，在下面输入一条开始。</li>';
    return;
  }
  threads.forEach(t => {
    const li = document.createElement('li');
    li.className = 'thread-item' + (t.thread_id === state.currentThreadId ? ' active' : '');
    const liveDot = t.in_progress ? '<span class="live-dot"></span>' : '';
    li.innerHTML = `
      <button type="button">
        <div class="thread-line-1">
          ${liveDot}
          <span class="thread-preview">${escapeHtml(t.preview || '(empty)')}</span>
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
  els.detailTitle.textContent = t.preview || '(empty)';
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
    const av = document.createElement('div');
    av.className = `mini-avatar ${avatarClass(name)}`;
    av.title = name;
    av.textContent = avatarLabel(name);
    els.detailParticipants.appendChild(av);
  });
}

function renderPosts(posts) {
  els.postList.innerHTML = '';
  const live = (posts || []).filter(p => !p.superseded);
  if (!live.length) {
    els.postList.innerHTML = '<div class="empty">这条 thread 还没有 post。</div>';
    return;
  }
  live.forEach(post => {
    const row = document.createElement('article');
    row.className = 'post';
    row.dataset.speaker = post.speaker;
    row.innerHTML = `
      <div class="avatar ${avatarClass(post.speaker)}">${escapeHtml(avatarLabel(post.speaker))}</div>
      <div class="post-content">
        <div class="post-head">
          <span class="post-name">${escapeHtml(post.speaker)}</span>
          <span class="post-time" title="${escapeHtml(post.ts || '')}">${escapeHtml(post.time || '')}</span>
        </div>
        <div class="post-body">${renderBody(post.content)}</div>
      </div>
    `;
    els.postList.appendChild(row);
  });
}

// ─── composer: new thread ─────────────────────────────────────────────
async function submitNewThread() {
  const content = els.threadComposerInput.value.trim();
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
  const content = els.postComposerInput.value.trim();
  if (!content || !state.currentThreadId) return;
  els.postComposerInput.disabled = true;
  els.btnSendPost.disabled = true;
  try {
    const updated = await apiJson(
      `/api/threads/${encodeURIComponent(state.currentThreadId)}/posts`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, speaker: 'scott' }),
      }
    );
    els.postComposerInput.value = '';
    autosize(els.postComposerInput);
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
        body: JSON.stringify({ closed_by: 'scott' }),
      }
    );
    renderDetail();
    refreshThreadsForCurrentSquad();
  } catch (err) {
    setStatus(`关闭失败: ${err.message}`, false);
  }
}

// ─── squad modal ──────────────────────────────────────────────────────
function openModal() {
  els.form.reset();
  [...els.memberCheckboxes.querySelectorAll('input')].forEach((input, idx) => {
    input.checked = idx === 0;
  });
  syncChairOptions();
  els.modal.classList.add('open');
  els.modal.setAttribute('aria-hidden', 'false');
  els.form.elements.id.focus();
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

function buildMemberControls() {
  els.memberCheckboxes.innerHTML = '';
  AGENTS.forEach(agent => {
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

function wireComposer(input, submit, counter) {
  input.addEventListener('input', () => {
    autosize(input);
    updateComposerCount(input, counter);
    updateMentionPicker(input);
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
  };
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
els.btnSendPost.onclick = submitPost;
els.btnCloseThread.onclick = closeCurrentThread;
els.btnRefreshThreads.onclick = refreshThreadsForCurrentSquad;
els.btnRefreshDetail.onclick = refreshCurrentThread;

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
loadSquads().then(() => { refreshAgentList(); startPolling(); });
