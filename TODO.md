# TODO

> Living backlog for OpenForge. Sorted by priority within each section.
> When something starts, link the PR. When it ships, move it to the changelog at the bottom or delete.

## 🔥 P0 — must-do before next dogfood round

- [x] **v0.9 — Agent Context Bundle**: STATUS.md per agent + auto-injected three-source bundle (status + main session tail + memory hits) on every openforge spawn. Endpoints `POST/PATCH/GET /api/agents/<id>/status`, `GET /api/agents/<id>/context-bundle`. See `docs/PRD-v0.9-agent-context-bundle.md`.

- [x] **Post routing**: when a `post_added` event with `speaker=scott` mentions one or more agents, spawn `openclaw agent` subprocesses sequentially and append each reply as a new `post_added` event. → `post_router.py` (single-worker serial queue, per-thread/agent stable session-id `forge-<tid>-<agent>`, errors recorded as `__router__` posts).
- [x] **Archive `run_standup.py`**: removed from tree; standup is no longer a first-class feature. Snapshot/restore logic lives in `agent_runtime.py` and is reused by `post_router`.
- [ ] Regression test: (a) snapshot a fake agent main, (b) clobber via the same path the post router uses, (c) confirm restore brings back the original sessionId.
- [ ] **Scheduled-thread templates**: a `forge-template` schema that pre-fills a thread opening post (the standup use-case returns as a thin layer).

## 🎯 P1 — product features (next 1–2 weeks)

- [x] **@agent picker in composer**: typing `@` opens an inline picker of the squad's members.
- [x] **Reply-to-post threading**: nested replies under a parent post (feature flag in settings).
- [x] **Reactions**: `reaction_added` / `reaction_removed` events + Slack-style hover bar + toggle chips.
- [x] **SSE / WebSocket push**: per-connection event tail of `events.jsonl` (8 s poll kept as fallback).
- [x] **Squad CRUD UI parity**: edit / archive / delete from the web.
- [ ] **Reopen / archive thread** semantics in the API + UI. _(P2, may not happen — Slack threads don't close; dogfood suggests we should drop the Close button instead and rely on last_post_at sort + search to surface live threads.)_
- [ ] **Cron integration**: docs + an example `cron` job that POSTs to `/api/squads/<id>/threads` on a schedule (uses the OpenClaw `cron` tool).
- [ ] **Per-thread main agent** so follow-ups don't always need an explicit `@`.

## 🛡 P1 — hardening & ops

- [ ] **Session pollution self-test on startup**: `server.py` prints a warning if any default agent's main is currently tainted.
- [ ] **Disk usage watcher**: warn if `events.jsonl` for a single thread exceeds 5 MB (tail rendering will get slow).
- [ ] **Failure replay**: a CLI to re-run only the failed `post_added` calls of a given thread (subprocess timeouts, agent errors).
- [ ] **Bearer token persistence**: when `--host 0.0.0.0`, store/load token from `~/.openclaw/openforge/server-token` so restarts don't break the bookmarked URL.
- [ ] **`bin/forge dev` self-background option** (post-#5 follow-up): add `forge dev --detach` that writes a pidfile and returns immediately, so humans who want fire-and-forget don't have to remember `nohup`. Pure UX — the router‑side hang was already fixed in #5 by killpg-on-timeout. Agents still must not call this, per [Dev service policy](README.md#dev-service-policy).
- [ ] **Router visibility for killed-on-timeout turns**: when `call_agent` raises with the new "process group killed" suffix, the `__router__` failure post should include a hint ("likely cause: agent spawned a long-lived process; see Dev service policy") instead of the raw error.

## 🧪 P2 — DX / tests

- [x] **Test suite**: pytest covering store / router / server smoke (2026-05-21).
- [x] **CI gates**: ruff lint + shellcheck + pytest matrix (3.11/3.12/3.13) + server boot smoke (2026-05-21).
- [x] **Auto-release**: green non-doc commit on main → date-based tag + GitHub Release (2026-05-21).
- [x] **Ruff config** wired into pyproject.toml (2026-05-21).
- [ ] Property tests for the projection (event log → meeting model is monotone).
- [ ] Widen pytest coverage (web/app.js via playwright in CI; agent_runtime snapshot/restore).
- [ ] **Playwright UI smoke harness** (post-#5 follow-up): headless, start→assert→exit within agent timeout. The only sanctioned way for a routed agent to verify UI behaviour end-to-end. Replaces the temptation to call `forge dev` from inside an agent turn.
- [ ] mypy minimal config (the codebase is small enough that this stays cheap).

## 🎨 P2 — UX polish

- [ ] Avatar generation for non-default agents (currently grey). Hash `agent_id` → palette slot.
- [ ] Dark mode (Slack-aware: dim sidebar, inverted post bubbles).
- [ ] Keyboard navigation (`j` / `k` to step posts, `Cmd+K` quick-switcher across squads / threads).
- [ ] Search across threads (regex over `events.jsonl` files; later FTS via SQLite).
- [ ] Markdown export per thread on demand (already auto-generated, but a one-click "Copy as Slack message" helper).

## 📚 P2 — docs & community

- [ ] **PRD** ✅ → `docs/PRD.md`
- [ ] Quickstart screencast (60 s).
- [ ] Architecture diagram (mermaid in `docs/architecture.md`).
- [ ] Compare-and-contrast page: OpenForge vs Multica vs Slack vs AutoGen GroupChat.
- [ ] Contributing guide (commit style is conventional commits; PRs ≤ 300 LOC).

## 🌌 P3 — future / speculative

- [ ] Event log on SQLite with `superseded_by` index (drop-in replacement once jsonl gets too slow).
- [ ] Multi-machine: the OpenClaw gateway already routes across nodes; lift the post router to RPC instead of subprocess.
- [ ] OpenProse integration: a `.prose` file that drives a thread (chair becomes the prose VM).
- [ ] Web UI for tweaking `manifest`-style thread templates (agenda, max turns, termination conditions).
- [ ] OpenForge "feeds": each agent gets a personal feed of threads they were @-ed on (Slack mentions tab equivalent).
- [ ] Read receipts per agent (which agent has actually consumed which post).

## ✅ Recently shipped

- 2026-05-26 — `fix(router): kill grandchildren on agent timeout to prevent in-flight deadlock` (#5)
- 2026-05-21 — `feat(ci): pytest + ruff + shellcheck + smoke + auto-release pipeline + CONTRIBUTING.md`
- 2026-05-21 — `feat(cli): forge service CLI + launchd integration` (`37b25e8`)
- 2026-05-21 — `polish(settings): redesign avatar editor` (`5614764`)
- 2026-05-21 — `feat(settings): personal avatar override for scott` (`6ee0343`)
- 2026-05-21 — `feat(router): implicit @ via reply to an agent post` (`77e0ae6`)
- 2026-05-21 — `feat(router): concurrent fan-out + per-(thread,agent) dedupe + 30min timeout` (`c61bf0b`)
- 2026-05-21 — `chore: retire run_standup CLI; extract agent_runtime.py` (`5ccb07e`)
- 2026-05-21 — `feat(router): agent replies inherit parent_post_id from trigger post` (`2749ad7`)
- 2026-05-21 — `feat(reactions): post hover bar + emoji chips + toggle` (`99ebd8a`)
- 2026-05-21 — `feat(router): agent replies inherit parent_post_id from trigger post` (`2749ad7`)
- 2026-05-21 — `chore: retire run_standup CLI; extract agent_runtime.py`
- 2026-05-20 — squad archive + reply nesting + settings modal + SSE (`57efe79` … `181f4ea`)
- 2026-05-19 — P0 post routing + squad CRUD + `--local --json` (`3b657a7` … `4fc542d`)
- 2026-05-16 — `feat: Slack-shaped threads (v0.4) — squad/thread/post API + middle & right composers, drop standup from UI`
- 2026-05-16 — `docs: PRD v0.2 (Slack-for-topics is P0; Linear-style tasks deferred to P1)`
- 2026-05-15 — `ci: add GitHub Actions workflow` (`a865cd9`)
- 2026-05-15 — `chore: add MIT LICENSE` (`e91e57a`)
- 2026-05-15 — `feat(brand): rename Huddle → OpenForge` (`21bff01`)
- 2026-05-15 — `refactor: rename huddle_store.py → forge_store.py` (`c6c0618`)
- 2026-05-15 — `docs/style/feat ui revamp pass` (codex, `c4349b0`–`b64a75f`, 7 commits)
- 2026-05-15 — `fix(run_standup): snapshot+restore agent main session pointers` (`8305436`)
- 2026-05-15 — `fix(rescue): restore_main_session.py` (`97a59ce`)
- 2026-05-15 — `chore: baseline v0.3 (jsonl + web viewer + run_standup)` (`4e55d48`)
