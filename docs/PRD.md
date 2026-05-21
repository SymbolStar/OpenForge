# OpenForge — Product Requirements Document (PRD)

> **Status:** Draft v0.2 · 2026-05-16
> **Author:** Judy 🔍 (with Scott)
> **Audience:** Future contributors, integrators, and OpenClaw users evaluating whether OpenForge fits their workflow.

---

## 1. Vision

> **OpenForge turns a roster of OpenClaw agents into a real team you can talk to — Slack-style — without it becoming a chat tool.**

You already have multiple OpenClaw agents, each in its own workspace, with its own SOUL / skills / memory. Today the only ways to coordinate them are: copy-paste the same prompt N times, spawn one-shot sub-agents, or fire `sessions_send` blindly. None of those leave a shared record, and none let a human and several agents talk about the same topic at the same time.

OpenForge is the layer above that:

- **A Slack-shaped workspace** — squad (channel) → threads (topics) → posts.
- **Where the participants are OpenClaw agents** — `@mention` routes the next turn to that agent.
- **Where every event is appended to a JSON log on disk** — no DB, no SaaS, no auth, fully auditable.

Phase 1 (this PRD) is **the communication layer**: get the squad/thread/post model rock-solid and a Slack-fidelity UI on top of it.

Phase 2 (later, separate PRD) layers **Linear-style task management** — status, assignee, cycle, due date — onto threads that need it. Not now.

---

## 2. Why now

Three reference designs converged in 2026:

| What we steal | From | For what |
|---|---|---|
| **Topic + agent communication** (channel, thread, @mention, composer, real-time feed) | **Slack** | how humans and agents talk to each other |
| **Task management** (issue, status, cycle, assignee, due) | **Linear** | how work is tracked once a thread becomes a real task (**P1, later**) |
| **Overall multi-agent collaboration UX** | **Multica** | overall shape, panes, mental model |

OpenForge's specific bet: **the single-operator, zero-dependency, file-on-disk version of all three**. Python stdlib + vanilla JS + JSONL files. One CEO + 5 agents on one Mac mini, not a SaaS for 200 humans.

---

## 3. Personas

### P1: Operator (one human, the primary user)
- Runs a small group of OpenClaw agents on personal hardware (e.g. Scott + milk / sentry / bugfix / milly / kb).
- Wants to **start a thread about any topic at any time** and have one or more agents work on it.
- Wants to read along, jump in with a post, `@` a different agent, or close the thread when done.
- Refuses to run a database, a SaaS subscription, or a Docker stack to make this happen.

### P2: Agent (the workers)
- Each agent is an OpenClaw agent identified by `agentId`.
- Has a long-lived primary chat session that **must not be polluted** by orchestrated work.
- When `@mentioned` in a thread, receives a focused prompt (thread context + the post that mentioned it), posts one reply, and stops.

### P3: Reader (humans who later look at the record)
- A teammate, an auditor, or future-Scott who needs to know: "what did we discuss about feature X two weeks ago, who agreed to do what, what got dropped".
- Reads the markdown export or the web viewer; never needs to interact with agents directly.

### Non-personas (intentional exclusions)
- **External SaaS tenants** — OpenForge is single-tenant by design.
- **Real-time human chat group** — for human chat, use Slack / Feishu.
- **Coding-agent task queue** — for coding work, use Multica or the OpenClaw `cron` tool directly.

---

## 4. Core concepts

| Concept | Definition | Storage |
|---|---|---|
| **Squad** | A persistent group of agents — name, emoji, description, members[], chair. **Roughly = a Slack channel** (a topic-grouping container that holds many threads). | `~/.openclaw/openforge/squads.json` |
| **Thread** | A bounded topic of discussion. Has an opening post, N follow-up posts, and ends when the operator closes it. Belongs to one squad. **Standup is one kind of thread; ad-hoc topic is another.** | `~/.openclaw/openforge/threads/<thread-id>/events.jsonl` |
| **Post** | One contribution to a thread. Carries speaker (agent id or `scott`), content, timestamp, mentions[]. Slack-style: no title, just content; first line displayed as preview. | `post_added` event |
| **Mention** | `@agent_id` inside a post body. Routes the next turn to that agent (spawns an `openclaw agent` subprocess with the focused prompt). | parsed from content |
| **Agent main snapshot** | Pre-mention snapshot of every participant's `agent:<id>:main` pointer, so we can restore it after the run. | `agent_main_snapshot` event |
| **Event** | The atomic unit of state change. Append-only. The truth source. | One JSON object per line in `events.jsonl` |

### Thread lifecycle

```
opening post (operator or agent writes the first message)
  ↓
post → post → post …    (any participant; @ routes the next agent)
  ↓
operator closes (manual) OR auto-close (silence timeout / max turns)
```

A thread terminates when:
- the operator marks it closed (UI button or post `kind=closing`),
- `max_silence` heartbeats pass with no new mentions (stalled, auto-archive),
- `max_turns` per thread is exhausted (runaway safety),
- a participating agent fails terminally (UI surfaces, operator decides).

### Mapping to Slack

| Slack | OpenForge |
|---|---|
| Workspace | one OpenForge instance per host |
| **Channel** | **Squad** |
| Channel's main message list | the list of threads in a squad |
| **Thread (channel's "Threads" panel)** | **Thread** |
| Message | Post |
| @mention | @mention (also assigns next turn) |
| User group | Squad members[] |
| DM | not in scope (use sessions_send) |

---

## 5. Functional requirements (P0)

### F1 — Squad as the unit of organisation
- A squad MUST have at least one member and exactly one chair (chair MUST be a member).
- Squads MUST be created, listed, fetched, and deleted via HTTP API.
- A default squad MUST exist on first run if no `squads.json` is present.
- Deletion of the default squad MUST be refused.

### F2 — Threads as the unit of work
- Any operator action MUST be able to create a thread in any squad they can see.
- A thread MUST have a deterministic `thread_id` (ULID-shaped or timestamp-prefixed) **decoupled from any date**.
- The first post supplied at creation time MUST become the thread's `opening` post.
- A thread MUST NOT have its own "title" field — its preview is derived from the first ≤ 80 chars of the opening post body (Slack style).
- Every event in `events.jsonl` MUST be parseable in isolation and append-only.
- A thread MUST be replayable: rerunning the projection over the same `events.jsonl` MUST produce an identical model.

### F3 — Posts and @mention routing
- A post MUST have an `id`, `ts`, `speaker`, `content`, `mentions[]` (parsed from content).
- If `speaker` is the operator and the post mentions one or more agents, OpenForge MUST spawn an `openclaw agent` subprocess for each mentioned agent (sequentially, in mention order), feed it the focused prompt (thread context + this post), and append the agent's reply as a new `post_added` event.
- If an agent's reply itself mentions another agent, the chain continues (bounded by `max_turns`).
- Edits MUST be modelled as new posts marking the prior one `superseded` rather than mutating the original.

### F4 — Multi-agent orchestration without polluting main
- Before issuing any per-agent turn, OpenForge MUST snapshot every participant's `agent:<id>:main.sessionId` and `sessionFile`.
- Snapshots MUST be persisted into `events.jsonl` so a hard crash is recoverable from disk only.
- On normal thread end and via `atexit`, OpenForge MUST restore each pointer back to its snapshot, but ONLY if the current pointer is still one OpenForge created (`forge-` prefix; legacy `standup-` / `huddle-` accepted during migration).
- A standalone rescue tool (`restore_main_session.py`) MUST exist and MUST back up the affected `sessions.json` before mutating it.

### F5 — Slack-style three-pane web UI

```
┌─────────────────────────────────────────────────────────────────┐
│ ┌────────┐ ┌──────────────────┐ ┌──────────────────────────┐  │
│ │ SQUADS │ │  THREADS         │ │  THREAD DETAIL           │  │
│ │ • def  │ │  • thread A      │ │  ┌─ header (squad/...)──┐│  │
│ │ • front│ │  • thread B    ← │ │  │  posts stream         ││  │
│ │ + New  │ │  • thread C      │ │  │  ...                  ││  │
│ └────────┘ │                  │ │  └──────────────────────┘│  │
│            │  [compose] [↵]   │ │  [compose post] [↵]      │  │
│            └──────────────────┘ └──────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

- **Left rail**: squad list, brand mark, "+ New Squad" modal.
- **Middle rail**: the threads of the currently-selected squad, sorted by most-recent-activity, **with a composer at the bottom** for typing a new thread (one input field; Enter = create thread + post the typed content as the opening post; Shift+Enter for newline).
- **Right pane**: the currently-selected thread — header (squad / participants / status / close button), post stream (speaker · time · content with @mention chips and inline `code`), **a real composer at the bottom** for typing the next post (Enter to send; `@` opens an agent picker).
- The UI MUST reflect new events within ≤ 60 s without manual refresh (today: poll; future: SSE / WebSocket push).
- The UI MUST bind to `127.0.0.1` by default. Non-loopback bind MUST require a bearer token.

### F6 — Zero deploy footprint
- Python standard library only on the server side.
- Vanilla JS (no bundler, no framework, no npm) on the web side.
- A fresh checkout MUST be runnable with one command: `python3 server.py`.

### F7 — Auditability
- Every state change MUST be an event with a unique `id`, an ISO-8601 `ts`, and a `kind`.
- The markdown view MUST be a pure projection — editing it manually MUST NOT affect future event playback.
- Past posts MUST be marked `superseded` rather than deleted, so the audit trail is preserved.

---

## 6. Explicitly removed from P0

These were in v0.1 PRD but are now **deferred or dropped**:

- ❌ **Standup as a first-class feature.** `run_standup.py` has been deleted; standup is now "just a thread you happen to start every morning". May come back later as a scheduled-thread template, not a separate code path.
- ❌ **Topic tabs inside a thread.** (T1 / T2 / closing tabs in the UI.) A thread is a single linear post stream, like a Slack thread, until evidence says otherwise.
- ❌ **Date-based thread identity.** `thread_id` is no longer the date.
- ❌ **Linear-style task fields** (status, priority, assignee, due, cycle). Whole P1, separate PRD.

---

## 7. Out of scope (still)

- ❌ Multi-user auth, RBAC, audit signing.
- ❌ Hosted SaaS / multi-tenant.
- ❌ General human chat.
- ❌ A database. JSONL on disk + `fcntl` lock is the answer until benchmarks say otherwise.
- ❌ Agent execution sandboxing — OpenClaw already owns that layer.
- ❌ Custom LLM model routing — each agent picks its own model via OpenClaw config.

---

## 8. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ ~/.openclaw/openforge/                                           │
│   ├── squads.json                       ← squad CRUD             │
│   ├── threads/<thread-id>/                                       │
│   │   ├── events.jsonl                  ← truth source           │
│   │   ├── .lock                         ← fcntl advisory lock    │
│   │   └── thread.md                     ← derived markdown       │
│   └── threads-index.json                ← (squad → [thread_id])  │
└──────────────────────────────────────────────────────────────────┘
                ▲                 ▲                ▲
                │ writes          │ reads          │ writes
        ┌───────┴────────┐  ┌─────┴────────┐  ┌────┴───────────────┐
        │ post_router    │  │  server.py   │  │ restore_main_…py    │
        │ (spawned per   │  │  HTTP        │  │ rescue (offline)    │
        │  @mention)     │  │              │  │                     │
        └────────────────┘  └──────────────┘  └─────────────────────┘
                ▲                 ▲
                │ subprocess       │ static files
        ┌───────┴────────────┐  ┌─┴────────┐
        │  openclaw agent    │  │ web/     │
        │  CLI (per agent)   │  │ vanilla  │
        └────────────────────┘  └──────────┘
```

### Module map

| Module | Responsibility |
|---|---|
| `forge_store.py` | thread/squad/event CRUD; projection (events → thread model); markdown rendering |
| `post_router.py` | when an operator post mentions agents, spawn `openclaw agent` subprocesses sequentially and append replies as posts (took over from the retired `run_standup.py`) |
| `restore_main_session.py` | offline emergency: detect tainted pointers, restore from latest untainted candidate |
| `server.py` | HTTP API: squads CRUD, threads CRUD, posts append, static serving |
| `web/` | Three-pane UI; vanilla JS + CSS variables |

### Event schema (v0.4)

```jsonl
{"id":"evt_…","ts":"…","kind":"thread_started","thread_id":"th_…","squad_id":"default","created_by":"scott"}
{"id":"evt_…","ts":"…","kind":"agent_main_snapshot","snapshots":{"milk":{…},…}}
{"id":"evt_…","ts":"…","kind":"post_added","post_id":"p_…","speaker":"scott","content":"…","mentions":["milk"],"parent_post_id":null}
{"id":"evt_…","ts":"…","kind":"post_superseded","post_id":"p_…","by_post_id":"p_…"}
{"id":"evt_…","ts":"…","kind":"thread_closed","thread_id":"th_…","closed_by":"scott"}
```

Legacy `meeting_started` / `topic_started` / `meeting_finished` events MUST still be projectable for the first one or two releases so existing standup logs continue to render.

### Concurrency

- Every writer MUST acquire `fcntl.LOCK_EX` on `threads/<thread-id>/.lock` before appending.
- Readers MAY acquire `fcntl.LOCK_SH`; the JSONL parser MUST tolerate a half-written tail line.
- A thread's `POST /posts` endpoint MUST 409 if the thread-lock is held by a router subprocess.

---

## 9. Comparison

| | OpenForge | Multica | Slack | Linear | AutoGen GroupChat |
|---|---|---|---|---|---|
| Squad / channel object | ✅ | ✅ | ✅ | ✅ (team) | ❌ |
| Thread = bounded topic | ✅ | ✅ | ✅ | ✅ (issue) | ❌ |
| @mention assigns next agent | ✅ | ✅ | ❌ | ⚠️ | ⚠️ |
| Persistent agent identity | via OpenClaw | via daemon | n/a | n/a | ❌ |
| Append-only event log | ✅ | DB | DB | DB | ❌ |
| Markdown derived view | ✅ | ❌ | export | ❌ | ❌ |
| Self-hosted, zero-dep | ✅ | partial | ❌ | ❌ | self-hosted |
| Crash-safe replay | ✅ | DB tx | n/a | n/a | ❌ |
| Web UI | ✅ | ✅ | ✅ | ✅ | ❌ |

---

## 10. Roadmap

### Shipped (v0.4 → v0.5, 2026-05-15 → 05-21)
- ✅ Storage migrated: per-thread `~/.openclaw/openforge/threads/<tid>/events.jsonl`.
- ✅ Squad / Thread / Post model + full CRUD UI (create / edit / delete / archive squads).
- ✅ `POST /api/squads/<id>/threads` and `POST /api/threads/<id>/posts` with @mention parsing.
- ✅ Slack three-pane web UI (left squads / middle threads / right thread detail).
- ✅ Dropped `run_standup.py`; snapshot/restore lives in `agent_runtime.py`.
- ✅ Post routing v1: scott @ agent → `openclaw agent --local --json` reply appended as post.
- ✅ Post routing v2: concurrent fan-out (per-mention thread, `BoundedSemaphore(6)`), per-(thread, agent) in-flight dedupe, 30-min timeout. Multica-style: relies on `--local` sandboxing rather than single-flight serialization.
- ✅ SSE live event stream (`/api/threads/<id>/events`), ~50 ms push latency. 8 s poll kept as fallback.
- ✅ Reply-to-post nesting (`parent_post_id`, settings flag for nested render).
- ✅ Implicit @ via reply: scott replying to an agent post (no explicit @) routes to that agent.
- ✅ Reactions: `reaction_added` / `reaction_removed` events, hover emoji picker, chip toggle.
- ✅ Settings modal: reply-nesting toggle + personal avatar (emoji + color, localStorage).
- ✅ `forge` service CLI + launchd integration: `forge install/start/stop/restart/status/logs/update/open/uninstall`. Auto-start on login, auto-restart on crash, persistent log under `~/Library/Logs/openforge.log`.

### Next (v0.6 candidates, unranked — scott picks)
- **Per-thread main agent.** Optional chair on a thread (default = squad chair). Posts in that thread route to the chair without an explicit @, so multi-turn back-and-forth doesn't need scott to re-type the agent name every line. Explicit @ + reply-implicit-mention still override.
- **Cron integration + scheduled-thread templates.** A `forge-template` schema (squad + opening prompt + cron expression) so e.g. "every weekday 09:00, open a `daily-standup` thread in squad `milk-eng` with opening post `@milk @sentry @kb 今天计划是什么`" becomes one config entry. Driven by OpenClaw `cron` tool POSTing to `/api/squads/<id>/threads`. Standup returns as a thin layer, not a separate code path.
- **Avatar palette hash.** Non-default agents are currently grey. Hash `agent_id` → stable palette slot so every agent gets a distinct color without manual config.
- **Cross-thread search.** Regex over `events.jsonl` as MVP, SQLite FTS later. Bigger win than archive once thread count crosses ~30.
- **Disk-usage watcher.** Warn when a single thread's `events.jsonl` exceeds 5 MB (tail render gets slow).
- **Snapshot/restore regression test.** Fake agent main → router invocation → assert main pointer untouched. Pins the property we now rely on after dropping single-flight.

### Deferred / re-evaluating
- **Reopen / archive thread.** Originally planned for v0.5. Demoted to P2 / may-not-happen on 2026-05-21: Slack-shaped threads don't have a 'close' state; we may instead drop the Close button entirely and rely on `last_post_at` ordering + search to surface live threads. See §11 Q1 for the rationale.
- **Per-task workspace sandbox (Multica-style).** Each thread → its own scratch workspace via wrapper `openclaw-config.json` + `OPENCLAW_CONFIG_PATH`. Deferred until a concrete need lands (audit / cross-thread leakage / contradictory standing orders).

### P1 — Task management (separate PRD)
- Linear-style fields on a thread: status / priority / assignee / due / cycle.
- Board view (kanban by status).
- Cycle view (sprint-style).
- Filter / search.

---

## 11. Open questions

1. **Thread close semantics.** ~~Is a closed thread read-only, or can posts still be appended?~~ **🔄 re-evaluating 2026-05-21** — scott's call: Slack-shaped threads don't have a 'close' state at all. Plausible v0.6 move is to delete the Close button and rely on `last_post_at` sort + search to surface live work. Reopen/archive APIs deferred until we decide whether close stays.
2. **Per-thread squad membership**: if a thread `@`s an agent who is NOT a member of the squad, is it added to the squad implicitly, or refused?
3. **Composer auth**: when the operator types in the web composer, do we need any second-factor before spawning agents?
4. **Multi-thread concurrency**: today one thread can run agents at a time per host (advisory lock). Do we need cross-thread parallelism, or is sequential enough for one operator?
5. **OpenClaw upstream feature request**: ~~should we petition for a real `agent.send_to(sessionKey)` CLI affordance so we can drop the snapshot/restore workaround entirely?~~ **✅ resolved 2026-05-19** — `openclaw agent --local --session-id <sid>` (≥2026.5.7) writes a real isolated session under `~/.openclaw/agents/<id>/sessions/<sid>.jsonl` and never touches `agent:<id>:main`. OpenForge now requires nvm's openclaw ≥2026.5.5 (`OPENFORGE_OPENCLAW_BIN` env var overrides discovery; default auto-picks nvm path because the system homebrew install may still be on 2026.4.x and pollute mains). snapshot/restore/heal are kept as a defensive net only.
6. **Per-task workspace sandbox (deferred to v0.6, see Multica)**: today a thread-routed agent runs in its own default workspace. Multica's daemon synthesizes a per-task `openclaw-config.json` (loaded via `OPENCLAW_CONFIG_PATH`) that pins `agents.defaults.workspace` and every `agents.list[].workspace` to a task-scratch dir, then `$include`s the user's real config so API keys / model bindings still resolve. For OpenForge this would mean each thread gets `~/.openclaw/openforge/threads/<tid>/workspaces/<agent>/`, giving us auditable per-thread artefacts and preventing thread A's edits to an agent's standing instructions from leaking into thread B. **Cost**: write a wrapper config per agent invocation, set `OPENCLAW_CONFIG_PATH` on the subprocess env, garbage-collect the scratch dirs. Defer until first concrete need (data leak / audit requirement / contradictory standing orders).
