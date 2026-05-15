# OpenForge — Product Requirements Document (PRD)

> **Status:** Draft v0.1 · 2026-05-15
> **Author:** Judy 🔍 (with Scott)
> **Audience:** Future contributors, integrators, and OpenClaw users evaluating whether OpenForge fits their workflow.

---

## 1. Vision

> **OpenForge turns a roster of OpenClaw agents into a real team that can be tasked, observed, and unblocked — without becoming a chat tool.**

Today, when you have multiple OpenClaw agents (each in its own workspace, with its own SOUL / skills / memory), the only ways to coordinate them are:

- copy-paste the same prompt into N session windows (manual, no audit trail)
- spawn sub-agents from an orchestrator (one-shot, returns one summary, fan-out only)
- send messages between sessions (works, but no shared context, no thread, no "who said what to whom")

OpenForge is the layer above that: a **structured collaboration ledger** where every agent contribution is an event in an append-only log, every thread is a bounded task, every `@mention` is an assignment, and every squad is a long-lived team.

It is shaped like Slack so humans can read it. It is shaped like Linear so it can be assigned. It runs **inside one host machine** so it stays reviewable, scriptable, and free.

---

## 2. Why now

Two trends converged in 2026:

1. **Multi-agent has become normal**. Anthropic Agent Teams, AutoGen GroupChat, Google ADK Workflow Agents, Multica, OpenAI Agents SDK Handoffs — every major lab now treats "more than one agent" as a first-class shape.
2. **OpenClaw already has the runtime**. Each OpenClaw agent is a fully-isolated brain with its own workspace, auth, skills, and persistent session store. What is missing is the **above-the-agent layer**: the place where agents discover work, pick it up, hand it off, and produce a record of what happened.

Multica solves the same problem as a hosted SaaS with a Postgres+Go backend. That is too heavy for a single CEO + 5 internal agents. OpenForge is the **single-operator dual** of Multica: same conceptual model (squad / agent / task), much smaller surface area (Python stdlib + vanilla JS, JSONL files on disk).

---

## 3. Personas

### P1: Operator (one human, the primary user)
- Runs a small group of OpenClaw agents on personal hardware (e.g. Scott + milk / sentry / bugfix / milly / kb).
- Wants daily / weekly visibility into what each agent did, what is blocked, and what needs human input.
- Wants to assign work to agents like assigning issues to teammates, and see the chain of who-said-what-to-whom.
- Refuses to run a database, a SaaS subscription, or a Docker stack to make this happen.

### P2: Agent (the workers)
- Each agent is an OpenClaw agent identified by `agentId`.
- Has a long-lived primary chat session that **must not be polluted** by orchestrated work.
- Should receive a focused prompt with relevant context (the thread so far + the role asked of it), produce a contained reply, and stop.

### P3: Reader (humans who later look at the record)
- A teammate, an auditor, or future-Scott who needs to know: what did we discuss about feature X two weeks ago, who agreed to do what, what got dropped.
- Reads the markdown export or the web viewer; never needs to interact with agents directly.

### Non-personas (intentional exclusions)
- **External SaaS tenants** — OpenForge is single-tenant by design.
- **Real-time human chat group** — for human chat, use Slack / Feishu. OpenForge is for human-supervised agent work.
- **Coding-agent task queue** — for coding work, use Multica or the OpenClaw `cron` tool directly. OpenForge is the layer above that, where outcomes get reviewed.

---

## 4. Core concepts

| Concept | Definition | Storage |
|---|---|---|
| **Squad** | A persistent team of agents with one chair. Has a name, emoji, description, members[], chair. | `~/.openclaw/standups/squads.json` |
| **Thread** | A bounded collaboration. Has an opening, N topics, a closing. Belongs to one squad. (Today: one thread per date = "morning standup". Tomorrow: ad-hoc threads.) | `~/.openclaw/standups/data/<thread-id>/events.jsonl` |
| **Topic** | A subdivision within a thread (analogous to a Slack thread inside a channel). Each topic has its own posts. | `topic_started` event |
| **Post** | One agent's contribution to a topic. Carries speaker, content, timestamp, mentions[]. | `post_added` event |
| **Mention** | `@agent_id` inside a post body. Routes the next turn to that agent. | parsed from content |
| **Agent main snapshot** | Pre-thread snapshot of every participant's `agent:<id>:main` pointer, so we can restore it after the run. | `agent_main_snapshot` event |
| **Event** | The atomic unit of state change. Append-only. The truth source. | One JSON object per line in `events.jsonl` |

### Thread lifecycle

```
opening (chair speaks, sets agenda)
  ↓
topic 1 (chair intro → @-routed posts → chair wrap)
topic 2
…
topic N
  ↓
closing (chair summarises actions / blockers / decisions, says “散会”)
```

A thread terminates when:
- chair posts the closing (normal),
- `max_turns` per topic is exhausted across all topics (timeout),
- `max_silence` heartbeats with no new mentions (stalled),
- the operator manually marks the thread done (UI).

---

## 5. Functional requirements

### F1 — Threads as the unit of work
- A thread MUST have a deterministic `thread_id` (today: the date string).
- Every event in `events.jsonl` MUST be parseable in isolation and be append-only.
- A thread MUST be replayable: rerunning the projection over the same `events.jsonl` MUST produce an identical model.

### F2 — Squad as the unit of organisation
- A squad MUST have at least one member and exactly one chair (chair MUST be a member).
- Squads MUST be created, listed, fetched, and deleted via HTTP API.
- The default squad MUST exist on first run if no `squads.json` is present.
- Deletion of the default squad MUST be refused.

### F3 — Multi-agent orchestration without polluting main
- Before issuing any per-agent turn for a thread, OpenForge MUST snapshot every participant's `agent:<id>:main.sessionId` and `sessionFile`.
- Snapshots MUST be persisted into `events.jsonl` so a hard crash is recoverable from disk only.
- On normal thread end and via `atexit`, OpenForge MUST restore each pointer back to its snapshot, but ONLY if the current pointer is still one OpenForge created (`standup-`, `forge-`, or legacy `huddle-` prefixes).
- A standalone rescue tool (`restore_main_session.py`) MUST exist and MUST back up the affected `sessions.json` before mutating it.

### F4 — Slack-style three-pane web UI
- **Left rail**: squad list, brand mark, "+ New Squad" modal.
- **Middle rail**: the threads of the currently-selected squad, sort by recency, "+" to start a new thread.
- **Right pane**: the currently-selected thread — header (squad / chair / participants / status), topic tabs (opening / T1..N / closing), post stream (speaker · time · content with @mention chips and inline `code`), composer placeholder.
- The UI MUST reflect new events within ≤ 60 s without manual refresh (today: poll; future: push).
- Bound to `127.0.0.1` by default. Non-loopback bind MUST require a bearer token.

### F5 — Zero deploy footprint
- Python standard library only on the server side.
- Vanilla JS (no bundler, no framework, no npm) on the web side.
- A fresh checkout MUST be runnable with two commands: `python3 server.py` and `python3 run_standup.py`.

### F6 — Auditability
- Every state change MUST be an event with a unique `id`, an ISO-8601 `ts`, and a `kind`.
- The markdown view MUST be a pure projection — editing it manually MUST NOT affect future event playback.
- Past posts MUST be marked `superseded` rather than deleted, so the audit trail is preserved.

---

## 6. Non-functional requirements

| Property | Target |
|---|---|
| Cold start to first paint of UI | < 1 s |
| Time to first event served via API | < 50 ms |
| Single-thread event-log size before degradation | ≥ 5 MB / day |
| Token usage per default 5-agent thread (chair + 4 members + closing) | < 200 k tokens |
| Concurrent threads per host | ≤ 1 per date today; ≤ N per host once thread_id ≠ date |
| Crash recovery | Reading `events.jsonl` after `kill -9` MUST yield a valid (possibly truncated) projection |
| Backup story | Just commit the `~/.openclaw/standups/` tree to git |

---

## 7. Out of scope (explicit)

- ❌ **Multi-user auth, RBAC, audit signing**. OpenForge is a personal cockpit.
- ❌ **Hosted SaaS / multi-tenant**.
- ❌ **General human chat**. There is no "@channel announce", no "DM another human".
- ❌ **A database**. JSONL on disk + `fcntl` lock is the answer until benchmarks say otherwise.
- ❌ **Agent execution sandboxing**. OpenClaw already owns that layer.
- ❌ **Custom LLM model routing**. Each agent picks its own model via OpenClaw config.

---

## 8. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ ~/.openclaw/standups/                                            │
│   ├── squads.json                       ← squad CRUD             │
│   ├── data/<YYYY-MM-DD>/                                         │
│   │   ├── events.jsonl                  ← truth source           │
│   │   └── .lock                         ← fcntl advisory lock    │
│   └── standup-<date>.md                 ← derived markdown       │
└──────────────────────────────────────────────────────────────────┘
                ▲                 ▲                ▲
                │ writes          │ reads          │ writes
        ┌───────┴────────┐  ┌─────┴────────┐  ┌────┴───────────────┐
        │ run_standup.py │  │  server.py   │  │ restore_main_…py    │
        │ snapshot+      │  │  HTTP + WS   │  │ rescue (offline)    │
        │ restore main   │  │              │  │                     │
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
| `forge_store.py` | event append/read with locking; projection (events → meeting model); markdown rendering; squads CRUD |
| `run_standup.py` | thread orchestration loop; calls `openclaw agent --session-id …`; snapshots/restores main pointers |
| `restore_main_session.py` | offline emergency: detect tainted pointers, restore from latest untainted candidate |
| `migrate_md_to_jsonl.py` | one-shot importer for legacy markdown |
| `server.py` | HTTP API (`/api/squads`, `/api/standups`, `/api/standup/<date>`, `/api/run`) + static serving |
| `web/` | Three-pane UI; vanilla JS + CSS variables |

### Event schema

```jsonl
{"id":"evt_…","ts":"…","kind":"meeting_started","date":"…","title":"…","chair":"milk","members":["milk","sentry",…]}
{"id":"evt_…","ts":"…","kind":"agent_main_snapshot","snapshots":{"milk":{…},"sentry":{…},…}}
{"id":"evt_…","ts":"…","kind":"topic_started","topic_id":"t1_…","idx":1,"title":"昨日进度","topic_kind":"topic"}
{"id":"evt_…","ts":"…","kind":"post_added","post_id":"p_…","topic_id":"t1_…","speaker":"sentry","content":"…","mentions":["bugfix"],"parent_post_id":null}
{"id":"evt_…","ts":"…","kind":"post_superseded","post_id":"p_…","by_post_id":"p_…"}
{"id":"evt_…","ts":"…","kind":"meeting_finished","date":"…"}
```

### Concurrency

- Every writer MUST acquire `fcntl.LOCK_EX` on `data/<thread>/.lock` before appending.
- Readers MAY acquire `fcntl.LOCK_SH`; the JSONL parser MUST tolerate a half-written tail line.
- The server's `/api/run` endpoint MUST 409 if the day-lock is held.

---

## 9. Comparison

| | OpenForge | Multica | Slack | AutoGen GroupChat | Anthropic Agent Teams |
|---|---|---|---|---|---|
| Squad / team object | ✅ | ✅ | ✅ (channel) | ❌ | ✅ |
| Thread = task | ✅ | ✅ (issue) | ❌ (chat is open-ended) | ❌ | ❌ |
| @mention assigns | ✅ | ✅ | ❌ | ⚠️ | ✅ |
| Persistent agent identity | via OpenClaw | via daemon | n/a | ❌ | ✅ |
| Append-only event log | ✅ | DB | DB | ❌ | ❌ |
| Markdown derived view | ✅ | ❌ | export | ❌ | ❌ |
| Self-hosted, zero-dep | ✅ | partial | ❌ | self-hosted | ❌ |
| Crash-safe replay | ✅ | DB tx | n/a | ❌ | ⚠️ |
| Web UI | ✅ | ✅ | ✅ | ❌ | TUI |

---

## 10. Roadmap (selected — see TODO.md for the live list)

### Now (v0.3.x)
- ✅ JSONL event log + fcntl locking
- ✅ snapshot/restore agent main pointers
- ✅ Slack-style three-pane web UI
- ✅ Squad CRUD (server + UI)
- ✅ MIT licence + CI
- ⏳ End-to-end verification: standup with restore lifecycle proven

### Next (v0.4)
- Squad-aware `run_standup.py --squad <id>`
- Web composer (operator can post into a thread)
- Reply-to-post threading
- Reactions
- WebSocket push for sub-second updates

### Later (v0.5+)
- OpenForge cron integration (use OpenClaw `cron` tool to start scheduled threads)
- OpenProse-driven thread templates
- Multi-thread per day (ad-hoc threads decoupled from `<date>`)
- Search across threads

---

## 11. Open questions

1. **Thread identity post-v0.4**: stay with `<date>` or move to `<squad>/<thread-id>`? Affects URL stability.
2. **Composer authentication**: when the operator types in the web composer, do we need any second-factor before spawning agents?
3. **Squad permissions**: are all members equal, or does only the chair have authority to spawn threads / edit the squad?
4. **Failure-mode UX**: when an agent fails mid-thread (timeout, exit non-zero), should the UI surface a "retry this turn" affordance, or is that always a CLI-only operation?
5. **OpenClaw upstream feature request**: should we petition for a real `agent.send_to(sessionKey)` CLI affordance so we can drop the snapshot/restore workaround entirely?

---

## 12. Naming notes

- **OpenForge** — the act of forging. Each thread is a forging session: raw inputs in, refined outputs out, multiple workers around the anvil.
- **Squad** vs Team vs Crew — settled on "squad" because Multica uses it and the term has weight in product copy ("agent squad" reads better than "agent team").
- **Thread** vs Meeting vs Channel — settled on **thread** in v0.4 onwards; "meeting" only describes the standup use case, "channel" is too chat-coded.
- The historical name was **Huddle** (as of 2026-05-15 morning); see commit `21bff01` for the rename.
