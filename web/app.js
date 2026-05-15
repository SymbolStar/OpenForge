// Huddle web app — squads UI, vanilla JS.

const AGENTS = ['milk', 'sentry', 'bugfix', 'milly', 'kb'];

const els = {
  squadList: document.getElementById('squad-list'),
  squadTitle: document.getElementById('squad-title'),
  squadDescription: document.getElementById('squad-description'),
  meetingList: document.getElementById('meeting-list'),
  btnNewSquad: document.getElementById('btn-new-squad'),
  btnRunSquad: document.getElementById('btn-run-squad'),
  btnRefresh: document.getElementById('btn-refresh'),
  statusDot: document.getElementById('status-dot'),
  statusText: document.getElementById('status-text'),
  modal: document.getElementById('squad-modal'),
  form: document.getElementById('squad-form'),
  btnCloseModal: document.getElementById('btn-close-modal'),
  btnCancelModal: document.getElementById('btn-cancel-modal'),
  memberCheckboxes: document.getElementById('member-checkboxes'),
  chairSelect: document.getElementById('chair-select'),
  threadTitle: document.getElementById('thread-title'),
  threadSub: document.getElementById('thread-sub'),
  meetingStatus: document.getElementById('meeting-status'),
};

const state = {
  squads: [],
  squadDetails: new Map(),
  currentSquadId: null,
  currentDate: null,
  currentMeeting: null,
  running: false,
};

function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function setStatus(text, ok = true) {
  els.statusText.textContent = text;
  els.statusDot.className = 'dot ' + (ok ? 'dot-ok' : 'dot-warn');
}

async function apiJson(url, options) {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

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
    renderSquads();
    await renderCurrentSquadShell();
    setStatus(`已加载 ${state.squads.length} 个 squad`);
  } catch (err) {
    setStatus(`加载失败: ${err.message}`, false);
  }
}

function renderSquads() {
  els.squadList.innerHTML = '';
  state.squads.forEach(squad => {
    const detail = state.squadDetails.get(squad.id);
    const count = detail?.meetings?.length || 0;
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

async function loadMeeting(date) {
  return apiJson(`/api/standup/${encodeURIComponent(date)}`);
}

async function renderCurrentSquadShell() {
  const detail = state.squadDetails.get(state.currentSquadId);
  const squad = detail?.squad || state.squads.find(s => s.id === state.currentSquadId);
  if (!squad) {
    els.squadTitle.textContent = 'No squads';
    els.squadDescription.textContent = '';
    els.meetingList.innerHTML = '';
    renderMeetingShell(null);
    return;
  }
  els.squadTitle.textContent = `${squad.emoji || '#'} ${squad.name || squad.id}`;
  els.squadDescription.textContent = squad.description || `${squad.chair} 主持 · ${squad.members.length} members`;
  renderMeetingList(detail?.meetings || []);
  const meetings = detail?.meetings || [];
  if (!meetings.some(meeting => meeting.date === state.currentDate)) {
    state.currentDate = meetings[0]?.date || null;
  }
  if (state.currentDate) {
    await selectMeeting(state.currentDate);
  } else {
    renderMeetingShell(null);
  }
}

async function selectSquad(id) {
  state.currentSquadId = id;
  state.currentDate = null;
  renderSquads();
  await renderCurrentSquadShell();
}

function renderMeetingList(meetings) {
  const sorted = [...meetings].sort((a, b) => b.date.localeCompare(a.date));
  els.meetingList.innerHTML = '';
  if (!sorted.length) {
    els.meetingList.innerHTML = '<li class="empty-row">No meetings yet.</li>';
    return;
  }
  sorted.forEach(meeting => {
    const li = document.createElement('li');
    li.className = 'meeting-item' + (meeting.date === state.currentDate ? ' active' : '');
    li.innerHTML = `
      <button type="button">
        <span class="meeting-live ${meeting.in_progress ? 'is-live' : ''}"></span>
        <span class="meeting-main">
          <span class="meeting-date">${escapeHtml(meeting.date)}</span>
          <span class="meeting-meta">${meeting.topic_count || 0} topics · ${meeting.post_count || 0} posts</span>
        </span>
      </button>
    `;
    li.querySelector('button').onclick = () => selectMeeting(meeting.date);
    els.meetingList.appendChild(li);
  });
}

function renderMeetingShell(meeting) {
  if (!meeting) {
    els.threadTitle.textContent = 'No meeting selected';
    els.threadSub.textContent = '';
    els.meetingStatus.textContent = 'idle';
    return;
  }
  els.threadTitle.textContent = meeting.title || meeting.date;
  els.threadSub.textContent = `${meeting.chair} · ${meeting.members.length} members · ${meeting.sections.length} sections`;
  els.meetingStatus.textContent = meeting.in_progress ? 'in progress' : 'done';
}

async function selectMeeting(date) {
  state.currentDate = date;
  const detail = state.squadDetails.get(state.currentSquadId);
  renderMeetingList(detail?.meetings || []);
  try {
    state.currentMeeting = await loadMeeting(date);
    renderMeetingShell(state.currentMeeting);
    setStatus(`已加载 ${date}`);
  } catch (err) {
    state.currentMeeting = null;
    renderMeetingShell(null);
    setStatus(`会议加载失败: ${err.message}`, false);
  }
}

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
  const selected = [...els.memberCheckboxes.querySelectorAll('input:checked')].map(input => input.value);
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

els.btnNewSquad.onclick = openModal;
els.btnCloseModal.onclick = closeModal;
els.btnCancelModal.onclick = closeModal;
els.modal.onclick = event => {
  if (event.target === els.modal) closeModal();
};

els.form.onsubmit = async event => {
  event.preventDefault();
  const members = [...els.memberCheckboxes.querySelectorAll('input:checked')].map(input => input.value);
  if (!members.length) {
    setStatus('至少选择一个 member', false);
    return;
  }
  const payload = {
    id: els.form.elements.id.value.trim(),
    name: els.form.elements.name.value.trim(),
    description: els.form.elements.description.value.trim(),
    emoji: els.form.elements.emoji.value.trim(),
    members,
    chair: els.chairSelect.value,
  };
  try {
    const squad = await apiJson('/api/squads', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.currentSquadId = squad.id;
    closeModal();
    await loadSquads();
  } catch (err) {
    setStatus(`创建失败: ${err.message}`, false);
  }
};

function setRunning(running) {
  state.running = running;
  els.btnRunSquad.disabled = running;
  els.btnRunSquad.textContent = running ? '...' : '+';
}

els.btnRefresh.onclick = loadSquads;
els.btnRunSquad.onclick = async () => {
  if (!state.currentSquadId || state.running) return;
  setRunning(true);
  setStatus('启动 meeting...');
  try {
    const res = await apiJson(`/api/squads/${encodeURIComponent(state.currentSquadId)}/run`, { method: 'POST' });
    state.currentDate = res.date;
    await loadSquads();
    setStatus(`已启动 ${res.date}`);
  } catch (err) {
    setStatus(`启动失败: ${err.message}`, false);
  } finally {
    setRunning(false);
  }
};

buildMemberControls();
loadSquads();
