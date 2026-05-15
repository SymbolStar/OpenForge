# TODO

> Living backlog for OpenForge. Sorted by priority within each section.
> When something starts, link the PR. When it ships, move it to the changelog at the bottom or delete.

## 🔥 P0 — must-do before next dogfood round

- [ ] **Run a full standup on v0.3.1** to verify `agent_main_snapshot` + restore actually keeps `agent:<id>:main` clean across the entire run lifecycle (success / failure / Ctrl-C).
- [ ] Add a regression test that (a) snapshots a fake agent main, (b) clobbers it via the same path `run_standup` uses, (c) confirms the atexit / explicit restore puts the original sessionId back.
- [ ] Decide canonical event filename: keep `standups/data/<date>/events.jsonl` or migrate to `forge/<squad>/<thread-id>/events.jsonl` (the second matches the new product framing of "thread = task" but breaks the legacy importer).

## 🎯 P1 — product features (next 1–2 weeks)

- [ ] **Squad-aware `run_standup.py`**: `--squad <id>` reads members/chair from `squads.json`; current behaviour stays as the default.
- [ ] **Web composer**: real "post as Scott" path. Writes a `post_added` event with `speaker: scott`, `actor_kind: human` and feeds the next agent automatically.
- [ ] **@assign in web**: clicking an `@mention` chip prompts to enqueue that agent; respects squad membership.
- [ ] **Reply-to-post threading**: nested replies under a parent post (`parent_post_id` already in the schema).
- [ ] **Reactions**: `reaction_added` / `reaction_removed` events; UI toolbar already has a placeholder.
- [ ] **WebSocket push**: replace 60 s poll with a per-connection event tail of `events.jsonl`.
- [ ] **Squad CRUD UI parity**: edit / archive / member toggle from the web (POST done; PATCH/DELETE flows need UI).
- [ ] **Cron integration**: docs + an example `cron` job to start a standup on a schedule (uses existing OpenClaw `cron` tool, not GitHub Actions).

## 🛡 P1 — hardening & ops

- [ ] **Concurrent-thread guard**: today the day-lock is per-date; once threads exist independent of a date, lock per `thread_id`.
- [ ] **Session pollution self-test on startup**: `server.py` prints a warning if any default agent's main is currently tainted.
- [ ] **Disk usage watcher**: warn if `events.jsonl` for a single date exceeds 5 MB (tail rendering will get slow).
- [ ] **Failure replay**: a CLI to re-run only the failed `post_added` calls of a given thread (subprocess timeouts, agent errors).
- [ ] **Bearer token persistence**: when `--host 0.0.0.0`, store/load token from `~/.openclaw/openforge/server-token` so restarts don't break the bookmarked URL.

## 🧪 P2 — DX / tests

- [ ] Unit tests for `forge_store.parse_topics_from_opening` edge cases (empty, malformed numbering, CJK).
- [ ] Property tests for the projection (event log → meeting model is monotone).
- [ ] Mock `openclaw agent` CLI in tests so `run_standup.py` can be exercised end-to-end without burning tokens.
- [ ] Ruff / black / mypy minimal config (the codebase is small enough that this stays cheap).

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
- [ ] Multi-machine: the OpenClaw gateway already routes across nodes; lift `run_standup` to RPC instead of subprocess.
- [ ] OpenProse integration: a `.prose` file that drives a thread (chair becomes the prose VM).
- [ ] Web UI for tweaking `manifest`-style thread templates (agenda, max turns, termination conditions).
- [ ] OpenForge "feeds": each agent gets a personal feed of threads they were @-ed on (Slack mentions tab equivalent).
- [ ] Read receipts per agent (which agent has actually consumed which post).

## ✅ Recently shipped

- 2026-05-15 — `ci: add GitHub Actions workflow` (`a865cd9`)
- 2026-05-15 — `chore: add MIT LICENSE` (`e91e57a`)
- 2026-05-15 — `feat(brand): rename Huddle → OpenForge` (`21bff01`)
- 2026-05-15 — `refactor: rename huddle_store.py → forge_store.py` (`c6c0618`)
- 2026-05-15 — `docs/style/feat ui revamp pass` (codex, `c4349b0`–`b64a75f`, 7 commits)
- 2026-05-15 — `fix(run_standup): snapshot+restore agent main session pointers` (`8305436`)
- 2026-05-15 — `fix(rescue): restore_main_session.py` (`97a59ce`)
- 2026-05-15 — `chore: baseline v0.3 (jsonl + web viewer + run_standup)` (`4e55d48`)
