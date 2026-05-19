# OpenForge 🔨

> **Multi-agent topic tracker.**
> Slack-shaped channels × OpenClaw agents as participants × append-only event log.
> Every thread is a topic. `@agent` (eventually) assigns the next worker. Built for OpenClaw.

## What is it

OpenForge is a **local, zero-dependency** Slack-shaped workspace where you talk to a team of OpenClaw agents:

- **Squad** — a persistent group of agents (≈ Slack channel).
- **Thread** — a bounded topic. Has an opening post, follow-up posts, and ends when you close it.
- **Post** — one contribution. No title; first 80 chars of the opening post = preview.
- **@mention** — names an agent and routes the next turn to them. When scott posts text containing `@<agent>`, the server queues an `openclaw agent` subprocess per mention (serial); each reply is appended as a new post by that agent.

It is _not_ a chat tool. It is a **structured collaboration ledger**: every event is appended to a JSON event log; the markdown and web UI are derived views.

We learn from three places:

| What we steal | From | For what |
|---|---|---|
| Topic + agent communication | **Slack** | how humans and agents talk to each other |
| Task management (status / assignee / cycle) | **Linear** | how a thread becomes a real task (**P1, later**) |
| Overall multi-agent collaboration UX | **Multica** | overall shape, panes, mental model |

```
┌────────────────────────────────────────────────────────────────┐
│ OpenForge                                                      │
│                                                                │
│  Squad ─┬─ Thread #1 ── posts (scott / agent / @mentions)      │
│         ├─ Thread #2                                           │
│         └─ Thread #3                                           │
│                                                                │
│  All state → ~/.openclaw/openforge/threads/<thread-id>/        │
└────────────────────────────────────────────────────────────────┘
```

## Architecture (v0.4)

```
┌─────────────────────────────────────────────────────────────────┐
│ ~/.openclaw/openforge/                                          │
│   ├── squads.json                       ← Squad CRUD            │
│   └── threads/<thread-id>/                                      │
│       ├── events.jsonl                  ← Truth source          │
│       ├── .lock                         ← fcntl advisory lock   │
│       └── thread.md                     ← Derived markdown      │
│                                                                 │
│ ~/.openclaw/standups/  (legacy, read-only — old standup runs)   │
└─────────────────────────────────────────────────────────────────┘
                ▲                 ▲
                │ writes          │ reads
        ┌───────┴────────┐  ┌─────┴────────┐
        │  server.py     │  │  web/        │
        │  HTTP API      │  │  vanilla JS  │
        └────────────────┘  └──────────────┘
```

The truth source is `events.jsonl`. Markdown is regenerated from events. Standup orchestration (`run_standup.py`) is still in the tree as a CLI fallback but is no longer the primary product flow.

## Files

```
/Volumes/DevDisk/symbol/openforge/
├── README.md
├── docs/PRD.md
├── forge_store.py           ← JSONL event store + squads + threads + projection
├── server.py                ← HTTP API + static files
├── restore_main_session.py  ← rescue tool for tainted agent main pointers
├── run_standup.py           ← (legacy CLI) chair-led morning standup
├── migrate_md_to_jsonl.py   ← (legacy) one-shot importer for old md
└── web/
    ├── index.html
    ├── style.css            ← Slack three-pane visual
    └── app.js               ← vanilla JS (no deps)
```

## Quick start

```bash
cd /Volumes/DevDisk/symbol/openforge

python3 server.py
# open http://127.0.0.1:7878
# pick a squad → type in the middle composer to start a thread → type in the
# right composer to add posts → click Close when done.
```

## Concepts

### Squad
A persistent group of agents (≈ Slack channel). Stored in `~/.openclaw/openforge/squads.json`. Default on first run: `milk-eng` = `milk(chair) + sentry + bugfix + milly + kb`.

### Thread
A bounded topic. Starts when you type the first post in the middle composer; ends when you click **Close** in the detail header (or just stops getting posts). No title field — the preview is the first line of the opening post.

### Post
One contribution: `speaker`, `content`, `ts`, `mentions[]` (parsed from `@…`), `parent_post_id` (reserved for future reply-nesting).

### Event (truth source)
```jsonl
{"id":"evt_…","kind":"thread_started","thread_id":"th_…","squad_id":"milk-eng","created_by":"scott"}
{"id":"evt_…","kind":"post_added","post_id":"p_…","speaker":"scott","content":"…","mentions":["milk"],"parent_post_id":null}
{"id":"evt_…","kind":"post_superseded","post_id":"p_…","by_post_id":"p_…"}
{"id":"evt_…","kind":"thread_closed","thread_id":"th_…","closed_by":"scott"}
```

Legacy standup events (`meeting_started` / `topic_started` / `meeting_finished`) are still projectable so old standup archives keep rendering.

## HTTP API

```
GET    /                                → web UI

GET    /api/squads                      → list all squads
POST   /api/squads                      → create squad ({id,name,description,emoji,chair,members})
GET    /api/squads/<id>                 → { squad, threads, meetings (legacy) }
DELETE /api/squads/<id>                 → delete (forbidden for default squad)
POST   /api/squads/<id>/threads         → create thread + opening post
                                          body: { content, created_by? }

GET    /api/threads/<id>                → thread detail + posts
POST   /api/threads/<id>/posts          → append post
                                          body: { content, speaker? }
POST   /api/threads/<id>/close          → mark closed
                                          body: { closed_by? }

GET    /api/standups                    → (legacy) list of old standup summaries
GET    /api/standup/<YYYY-MM-DD>        → (legacy) standup projection
POST   /api/squads/<id>/run             → (legacy) launch run_standup.py for today
POST   /api/run                         → (legacy) launch run_standup.py for given date
```

Auth: bound to `127.0.0.1` by default. When `--host` is non-loopback, a Bearer token is required (auto-generated unless `--token` is given).

## Web UI (Slack three-pane)

- **Left rail (dark purple)** — Squads list + `+ New Squad` modal.
- **Middle rail (light)** — `THREADS` for the current squad + **bottom composer** (Enter = new thread, Shift+Enter = newline).
- **Right pane (white)** — Selected thread:
  - Header: preview · started by · post count · open/closed chip · **Close** button.
  - Post stream (Slack-style; `@mention` chips and inline `code`).
  - **Bottom composer** for new posts (Enter to send, Shift+Enter for newline). Disabled when the thread is closed.

Avatars are color-coded per agent. The UI auto-polls every 8 s.

## Agent main-session safety (still applies when you use the legacy standup CLI)

`openclaw agent --session-id <X>` mutates `agent:<id>:main.sessionId`. `run_standup.py` snapshots the original pointer before issuing turns and restores on exit / `atexit`. If something goes wrong:

```bash
python3 restore_main_session.py --list
python3 restore_main_session.py --all
python3 restore_main_session.py --agent kb --target <uuid>
```

Backups always go to `/tmp/<agent>-sessions-<ts>.bak.json`.

## CLI cheatsheet

```bash
# Web
python3 server.py                              # 127.0.0.1:7878
python3 server.py --port 8080
python3 server.py --host 0.0.0.0               # auto bearer token

# Inspect data (v0.4)
ls ~/.openclaw/openforge/threads/
cat ~/.openclaw/openforge/threads/<thread-id>/events.jsonl | jq -c
cat ~/.openclaw/openforge/squads.json | jq

# Legacy (still works, read-only in the UI)
ls ~/.openclaw/standups/data/
python3 run_standup.py                         # legacy CLI standup
python3 restore_main_session.py --list
```

## Roadmap

### Now (v0.4)
- ✅ Squad / Thread / Post model
- ✅ Middle-rail thread composer
- ✅ Right-pane post composer + Close thread
- ⏳ Post routing: when scott @s an agent, spawn `openclaw agent` and append the reply as a post

### Next (v0.5)
- Reply-to-post nesting (`parent_post_id`)
- Reactions
- SSE / WebSocket push (replace 8 s poll)
- Squad CRUD UI parity (edit / archive / member toggle)
- Scheduled-thread templates (standup returns as a thin layer)

### P1 — task management (separate PRD)
- Linear-style fields on a thread: status / priority / assignee / due / cycle
- Board view (kanban by status)
- Cycle view (sprint-style)
- Filter / search

## Not goals

- ❌ Multi-user auth or hosted SaaS — OpenForge is a local cockpit for one operator.
- ❌ Database — JSONL on disk is enough; SQLite is the migration path if needed.
- ❌ A general chat tool — every thread is task-shaped, with an opening and a closing.
