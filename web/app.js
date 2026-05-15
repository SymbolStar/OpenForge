// Huddle web app v0.3 — vanilla JS

const els = {
  meetingList:  document.getElementById('meeting-list'),
  meetingTitle: document.getElementById('meeting-title'),
  meetingMeta:  document.getElementById('meeting-meta'),
  topicList:    document.getElementById('topic-list'),
  threadTitle:  document.getElementById('thread-title'),
  threadSub:    document.getElementById('thread-sub'),
  threadList:   document.getElementById('thread-list'),
  btnRun:       document.getElementById('btn-run'),
  btnRefresh:   document.getElementById('btn-refresh'),
  statusDot:    document.getElementById('status-dot'),
  statusText:   document.getElementById('status-text'),
};

const state = {
  meetings: [],
  currentDate: null,
  currentMeeting: null,
  currentSectionIdx: 0,
  pollTimer: null,
  running: false,
};

// agent id allows ASCII word chars + dashes + CJK
const MENTION_RE = /@([\w\-\u4e00-\u9fff]+)/g;
const KNOWN_AGENTS = ['milk', 'sentry', 'bugfix', 'milly', 'kb', 'judy'];
const AGENT_GLYPH = {
  milk: '🥛', sentry: '🛡', bugfix: '🔧',
  milly: '🛠', kb: '📚', judy: '🔍',
};

// ─── helpers ────────────────────────────────────────────────────────
function avatarClass(speaker) {
  return `av-${KNOWN_AGENTS.includes(speaker) ? speaker : 'default'}`;
}
function initial(speaker) {
  if (AGENT_GLYPH[speaker]) return AGENT_GLYPH[speaker];
  // grab first non-whitespace code point, safe for CJK
  return [...(speaker || '?')][0].toUpperCase();
}
function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function renderBody(text) {
  let html = escapeHtml(text);
  html = html.replace(MENTION_RE,
    (_, name) => `<span class="mention">@${escapeHtml(name)}</span>`);
  html = html.replace(/`([^`\n]+)`/g,
    (_, code) => `<code>${escapeHtml(code)}</code>`);
  return html;
}
function setStatus(text, ok = true) {
  els.statusText.textContent = text;
  els.statusDot.className = 'dot ' + (ok ? 'dot-ok' : 'dot-warn');
}
function totalPosts(m) {
  return (m?.sections || []).reduce((a, s) => a + s.posts.length, 0);
}

// ─── data fetch ─────────────────────────────────────────────────────
async function loadMeetings() {
  setStatus('加载中…');
  try {
    const r = await fetch('/api/standups');
    state.meetings = await r.json();
    renderMeetingList();
    if (state.meetings.length) {
      const target = state.currentDate || state.meetings[0].date;
      await selectMeeting(target);
    }
    setStatus(`已加载 ${state.meetings.length} 个会议`);
  } catch (e) {
    setStatus('加载失败: ' + e.message, false);
  }
}

async function loadMeeting(date) {
  const r = await fetch(`/api/standup/${date}`);
  if (!r.ok) throw new Error(`load failed: ${r.status}`);
  return r.json();
}

// ─── rendering ─────────────────────────────────────────────────────
function renderMeetingList() {
  els.meetingList.innerHTML = '';
  if (!state.meetings.length) {
    const li = document.createElement('li');
    li.style.cssText = 'cursor:default;color:#b6b0c2;font-size:12px;';
    li.textContent = '暂无会议';
    els.meetingList.appendChild(li);
    return;
  }
  state.meetings.forEach(m => {
    const li = document.createElement('li');
    if (m.date === state.currentDate) li.className = 'active';
    const live = m.in_progress ? ' · 进行中' : '';
    li.innerHTML = `
      <div class="mt-date">${escapeHtml(m.date)}</div>
      <div class="mt-meta">${m.topic_count || 0} topics · ${m.members.length} 人${live}</div>
    `;
    li.onclick = () => selectMeeting(m.date);
    els.meetingList.appendChild(li);
  });
}

function renderTopicList() {
  const meeting = state.currentMeeting;
  els.meetingTitle.textContent = meeting.title || meeting.date;
  els.meetingMeta.textContent =
    `主席 ${meeting.chair} · ${meeting.members.length} 人参会${
      meeting.in_progress ? ' · 进行中' : ''}`;

  els.topicList.innerHTML = '';
  if (!meeting.sections.length) {
    const li = document.createElement('li');
    li.style.cssText = 'cursor:default;color:#999;font-size:12px;';
    li.textContent = '还没有 topic';
    els.topicList.appendChild(li);
    return;
  }
  meeting.sections.forEach((sec, idx) => {
    const li = document.createElement('li');
    if (idx === state.currentSectionIdx) li.className = 'active';
    const icon = { topic: '#', opening: '✦', closing: '✓', other: '·' }[sec.kind] || '·';
    li.innerHTML = `
      <div class="topic-icon">${icon}</div>
      <div>
        <div class="topic-title">${escapeHtml(sec.title)}</div>
        <div class="topic-count">${sec.posts.length} 条发言</div>
      </div>
    `;
    li.onclick = () => {
      state.currentSectionIdx = idx;
      renderTopicList();
      renderThread();
    };
    els.topicList.appendChild(li);
  });
}

function renderThread() {
  const meeting = state.currentMeeting;
  const sec = meeting.sections[state.currentSectionIdx];
  if (!sec) {
    els.threadTitle.textContent = '空';
    els.threadSub.textContent = '';
    els.threadList.innerHTML = '<div class="empty">这个会议还没有内容</div>';
    return;
  }
  els.threadTitle.textContent = sec.title;
  els.threadSub.textContent = `${sec.posts.length} 条发言 · ${meeting.date}`;

  if (!sec.posts.length) {
    els.threadList.innerHTML = '<div class="empty">这个 topic 还没有发言</div>';
    return;
  }

  els.threadList.innerHTML = '';
  sec.posts.forEach(p => {
    if (p.superseded) return;
    const div = document.createElement('div');
    div.className = 'post';

    const av = document.createElement('div');
    av.className = `avatar ${avatarClass(p.speaker)}`;
    av.textContent = initial(p.speaker); // safe: textContent

    const right = document.createElement('div');
    const head = document.createElement('div');
    head.className = 'post-head';
    const name = document.createElement('span');
    name.className = 'post-name';
    name.textContent = p.speaker;
    const time = document.createElement('span');
    time.className = 'post-time';
    time.textContent = p.time;
    head.appendChild(name);
    head.appendChild(time);

    const body = document.createElement('div');
    body.className = 'post-body';
    body.innerHTML = renderBody(p.content); // mentions/code already escaped

    right.appendChild(head);
    right.appendChild(body);
    div.appendChild(av);
    div.appendChild(right);
    els.threadList.appendChild(div);
  });
}

async function selectMeeting(date) {
  state.currentDate = date;
  setStatus(`加载会议 ${date}…`);
  try {
    state.currentMeeting = await loadMeeting(date);
    state.currentSectionIdx = 0;
    renderMeetingList();
    renderTopicList();
    renderThread();
    setStatus(
      `${date} · ${state.currentMeeting.sections.length} sections · ${
        totalPosts(state.currentMeeting)} posts`);
  } catch (e) {
    setStatus('加载失败: ' + e.message, false);
  }
}

// ─── actions ───────────────────────────────────────────────────────
function clearPollTimer() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}
function setRunning(running) {
  state.running = running;
  els.btnRun.disabled = running;
  els.btnRun.textContent = running ? '⏳ 开会中' : '▶ 开会';
}

els.btnRun.onclick = async () => {
  if (state.running) return;
  if (!confirm('要让 milk 起一个新的早会吗？大概要 5-15 分钟。')) return;
  setRunning(true);
  setStatus('启动中…');
  try {
    const r = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.started) {
      setStatus('启动失败: ' + (j.error || `HTTP ${r.status}`), false);
      setRunning(false);
      return;
    }
    setStatus(`正在开会，每 30s 自动刷新 ${j.date}`);
    clearPollTimer();
    state.currentDate = j.date;
    state.pollTimer = setInterval(async () => {
      await loadMeetings();
      try {
        const fresh = await loadMeeting(j.date);
        state.currentMeeting = fresh;
        renderMeetingList();
        renderTopicList();
        renderThread();
        if (!fresh.in_progress) {
          setStatus(`${j.date} 散会，共 ${totalPosts(fresh)} 条发言`);
          clearPollTimer();
          setRunning(false);
        }
      } catch {}
    }, 30000);
    // safety: stop after 25 min regardless
    setTimeout(() => { clearPollTimer(); setRunning(false); }, 25 * 60 * 1000);
  } catch (e) {
    setStatus('启动失败: ' + e.message, false);
    setRunning(false);
  }
};

els.btnRefresh.onclick = async () => {
  if (state.currentDate) {
    setStatus('刷新中…');
    await loadMeetings();
    await selectMeeting(state.currentDate);
  } else {
    await loadMeetings();
  }
};

// quiet auto-poll for in-flight meetings
setInterval(async () => {
  if (!state.currentDate) return;
  try {
    const fresh = await loadMeeting(state.currentDate);
    const oldCount = totalPosts(state.currentMeeting);
    const newCount = totalPosts(fresh);
    if (newCount > oldCount) {
      state.currentMeeting = fresh;
      renderTopicList();
      renderThread();
      setStatus(`+${newCount - oldCount} 条新发言`);
    }
  } catch {}
}, 60000);

// ─── bootstrap ────────────────────────────────────────────────────
loadMeetings();
