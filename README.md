# OpenForge 🔨

> **Multi-agent task tracker.**
> Slack-like threads × Linear-like task assignment × Multica-style squad.
> Every thread is a task. `@agent` assigns the next worker. Built for OpenClaw.

## What is it

OpenForge is a **local, zero-dependency** workspace where you orchestrate a team of OpenClaw agents:

- **Squad** — a fixed crew (chair + members) that handles a class of work
- **Thread** — a task. Has an opening, topics, posts, decisions, closing.
- **Post** — one strike of the hammer. Each agent's contribution to a thread.
- **@mention** — assigns the next agent to act in this thread.

It is _not_ a chat tool. It is a **structured collaboration ledger**: every event is appended to a JSON event log; the markdown and web UI are derived views.

```
┌────────────────────────────────────────────────────────────────┐
│ OpenForge                                                      │
│                                                                │
│  Squad ─┬─ Thread #1 ── posts (chair / agent / @ / decisions) │
│         ├─ Thread #2                                           │
│         └─ Thread #3                                           │
│                                                                │
│  All state → ~/.openclaw/standups/data/<date>/events.jsonl    │
└────────────────────────────────────────────────────────────────┘
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ ~/.openclaw/standups/                                           │
│   ├── squads.json                       ← Squad CRUD            │
│   ├── data/<YYYY-MM-DD>/                                        │
│   │   ├── events.jsonl                  ← Truth source          │
│   │   └── .lock                         ← fcntl advisory lock   │
│   └── standup-<date>.md                 ← Derived markdown view │
└─────────────────────────────────────────────────────────────────┘
                ▲                 ▲
                │ writes          │ reads
        ┌───────┴────────┐  ┌─────┴────────┐
        │ run_standup.py │  │  server.py   │  → web UI (vanilla JS)
        │ snapshot+      │  │  HTTP API    │
        │ restore main   │  │              │
        └────────────────┘  └──────────────┘
```

The truth source is `events.jsonl`. Markdown is regenerated atomically after every event.

## Files

```
/Volumes/DevDisk/symbol/openforge/
├── README.md
├── forge_store.py           ← JSONL event store + squads CRUD + projection
├── run_standup.py           ← chair-led morning standup (snapshots+restores main pointers)
├── restore_main_session.py  ← rescue tool for tainted agent main pointers
├── server.py                ← HTTP API + static files
├── migrate_md_to_jsonl.py   ← one-shot importer for legacy md
└── web/
    ├── index.html
    ├── style.css            ← Slack three-pane visual
    └── app.js               ← vanilla JS (no deps)
```

## Quick start

```bash
cd /Volumes/DevDisk/symbol/openforge

# 1. (optional) import any legacy standup-*.md into the new event log
python3 migrate_md_to_jsonl.py

# 2. start the web server
python3 server.py
# open http://127.0.0.1:7878

# 3. trigger a thread (= morning standup) for today
python3 run_standup.py
# or click "▶" in the web UI
```

## Concepts

### Squad
A fixed group of agents with a chair. Stored in `squads.json`. Default squad on first run: `milk-eng` = `milk(chair) + sentry + bugfix + milly + kb`.

### Thread (= meeting / task)
A bounded collaboration. Has phases: `opening` → topic 1..N → `closing`. Each phase has posts. Threads belong to one squad.

### Post
One agent's contribution to a thread topic. Posts can `@mention` other members; mentions are routed by the orchestrator to bring the next agent into the thread.

### Event (truth source)
```jsonl
{"id":"evt_…","kind":"meeting_started","date":"…","chair":"milk","members":[…]}
{"id":"evt_…","kind":"agent_main_snapshot","snapshots":{"milk":{"sessionId":"…"}, …}}
{"id":"evt_…","kind":"topic_started","topic_id":"t1_…","title":"昨日进度","topic_kind":"topic"}
{"id":"evt_…","kind":"post_added","post_id":"p_…","topic_id":"t1_…","speaker":"sentry","content":"…","mentions":["bugfix"]}
{"id":"evt_…","kind":"post_superseded","post_id":"p_…","by_post_id":"p_…"}
{"id":"evt_…","kind":"meeting_finished","date":"…"}
```

## Agent main-session safety (P0)

`openclaw agent --session-id <X>` **mutates** `agent:<id>:main.sessionId` to `<X>`. This is OpenClaw's documented behaviour — the CLI has no affordance for issuing a turn into an isolated session key.

OpenForge mitigates by:

1. **Snapshot** every participant's main pointer before the thread starts (also written into `events.jsonl` as `agent_main_snapshot` so a hard crash is recoverable).
2. **Restore** on normal exit + `atexit` hook, only when current main is tainted (sessionId starts with `standup-` / `forge-` / `huddle-`).
3. **Rescue tool** `restore_main_session.py` for emergency recovery:

```bash
python3 restore_main_session.py --list           # diagnose tainted vs OK
python3 restore_main_session.py --all            # restore all default agents
python3 restore_main_session.py --agent kb \
                                --target <uuid>  # explicit
```

Backups always go to `/tmp/<agent>-sessions-<ts>.bak.json`.

## HTTP API

```
GET  /                             → web UI
GET  /api/squads                   → list all squads
POST /api/squads                   → create squad ({id,name,description,emoji,chair,members})
GET  /api/squads/<id>              → squad detail + meetings
DELETE /api/squads/<id>            → delete (forbidden for default squad)
POST /api/squads/<id>/run          → trigger run_standup.py for that squad

GET  /api/standups                 → list all meetings (cross-squad, legacy)
GET  /api/standup/<YYYY-MM-DD>     → meeting projection for one date
POST /api/run                      → trigger run_standup.py for today (legacy)
```

Auth: bound to `127.0.0.1` by default. When `--host` is non-loopback, a Bearer token is required (auto-generated unless `--token` given).

## Web UI (Slack-style three pane)

- **Left rail (dark purple)** — Squads list + `+ New Squad` modal
- **Middle rail (white)** — Threads in the current squad + `▶` start new
- **Right pane (white)** — Current thread: header (squad / chair / participants / status) → tabs (opening / T1 / T2 / T3 / closing) → posts (Slack-bubble style; @mention chips, hover reactions/reply placeholders) → composer (placeholder)

Avatars are colored per agent. `@mentions` render as blue chips. The UI auto-polls in-flight threads every 60 s.

## CLI cheatsheet

```bash
# Threads (= meetings)
python3 run_standup.py
python3 run_standup.py --date 2026-05-15
python3 run_standup.py --members milk,sentry --chair milk

# Web
python3 server.py                              # 127.0.0.1:7878
python3 server.py --port 8080
python3 server.py --host 0.0.0.0               # auto bearer token

# Inspect data
ls ~/.openclaw/standups/data/
cat ~/.openclaw/standups/data/<date>/events.jsonl | jq -c
cat ~/.openclaw/standups/squads.json | jq
cat ~/.openclaw/standups/standup-<date>.md     # derived view

# Recover from a tainted main pointer
python3 restore_main_session.py --list
python3 restore_main_session.py --all
```

## Roadmap

- [ ] Threads beyond standups (ad-hoc task threads launched from web)
- [ ] Real composer: post a `speaker=scott` event from web
- [ ] Reply-to-post (nested threads under a post)
- [ ] Reactions (`reaction_added` event)
- [ ] WebSocket push (replace 60 s poll)
- [ ] Squad-aware `run_standup.py --squad <id>`
- [ ] Cross-thread search by agent / keyword
- [ ] PDF / image export

## Not goals

- ❌ Multi-user auth or hosted SaaS — OpenForge is a local cockpit for one operator
- ❌ Database — the JSONL log is enough; SQLite is the migration path if needed
- ❌ A general chat tool — every thread is task-shaped, with an opening and a closing
