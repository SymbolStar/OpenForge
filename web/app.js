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
};

const state = {
  squads: [],
  squadDetails: new Map(),
  currentSquadId: null,
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
    renderCurrentSquadShell();
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

function renderCurrentSquadShell() {
  const detail = state.squadDetails.get(state.currentSquadId);
  const squad = detail?.squad || state.squads.find(s => s.id === state.currentSquadId);
  if (!squad) {
    els.squadTitle.textContent = 'No squads';
    els.squadDescription.textContent = '';
    els.meetingList.innerHTML = '';
    return;
  }
  els.squadTitle.textContent = `${squad.emoji || '#'} ${squad.name || squad.id}`;
  els.squadDescription.textContent = squad.description || `${squad.chair} 主持 · ${squad.members.length} members`;
  els.meetingList.innerHTML = '<li class="empty-row">Meetings will appear here.</li>';
}

function selectSquad(id) {
  state.currentSquadId = id;
  renderSquads();
  renderCurrentSquadShell();
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

els.btnRefresh.onclick = loadSquads;
els.btnRunSquad.onclick = () => alert('Meeting run will be wired in the meetings rail.');

buildMemberControls();
loadSquads();
