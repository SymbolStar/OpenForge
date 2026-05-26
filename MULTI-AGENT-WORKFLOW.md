# Multi-Agent Workflow (v0.5)

This is how multiple AI agents (judy, dora, alice, …) share one OpenForge repo
without stepping on each other.

## TL;DR

- Every code change goes through a **worktree + branch + PR**.
- One agent can have **multiple worktrees in parallel** (one per task).
- The **`openforge-worktree`** CLI handles creation and cleanup; agents never
  call raw `git worktree` themselves.
- The rule is also injected automatically into every OpenForge thread's
  context bundle (`post_router.py → _build_routed_prompt`), so any
  participating agent sees it without per-thread setup.

## Layout

```
/Volumes/DevDisk/symbol/openforge/                  # main repo (branch: main)
/Volumes/DevDisk/symbol/openforge.worktrees/        # all per-task worktrees
    judy-multiagent-workflow/                       #   judy/multiagent-workflow
    dora-design-tokens/                             #   dora/design-tokens
    alice-router-tests/                             #   alice/router-tests
```

Branch naming: `<agent>/<task-slug>`.
Worktree path: `<repo>.worktrees/<agent>-<task-slug>/`.

## Lifecycle

### Create (when you accept a coding task)

```bash
openforge-worktree add <agent> <task-slug>
# stdout: absolute path to the new worktree (cd there to start)
# stderr: human progress lines
```

What it does:

1. `git fetch origin` (so you start from current main).
2. `git worktree add -b <agent>/<task-slug> <path> origin/main`
   (or reuses an existing branch if one already exists).
3. Prints the worktree path on stdout so callers can `cd "$(openforge-worktree add …)"`.

### Work

```bash
cd <worktree>
# edit, run tests, commit normally
git push -u origin <agent>/<task-slug>
gh pr create --title "[<agent>] <one-line summary>" \
  --body "thread: <openforge thread link>"
```

PR title **must** start with `[<agent>] ` so scott can scan reviews by author.

### Destroy (after PR is merged or abandoned)

```bash
openforge-worktree rm <agent>/<task-slug>
```

What it does:

1. `git worktree remove --force <path>` (deletes the directory).
2. `git branch -d <branch>` locally (leaves remote branch alone — GitHub
   handles that on merge).

### Inspect

```bash
openforge-worktree ls
```

Wraps `git worktree list`.

## Don't create a worktree when…

- You're only **reading** code or doing a code review.
- You're writing notes / docs into your own `~/.openclaw/workspace-<agent>/`.
- You're doing **temporary exploration** that won't be committed.
- Scott explicitly says "just commit on main".

Worktrees are for "I'm going to push commits". Skip them otherwise — they pile
up otherwise.

## Conflict handling

The rule itself (worktree + PR) is the primary conflict prevention: every
change lands through a PR scott reviews on GitHub, so any conflict surfaces
there, not on main.

Optional hardening (not implemented yet, listed for the next pass):

- **Pre-create awareness**: when `openforge-worktree add` runs, check
  `git log --all --since=1d` and warn if another agent touched the likely
  files recently.
- **Pre-push hook**: `git fetch && git log HEAD..origin/main` — warn if main
  has moved since the branch was created; suggest a rebase.
- **TTL sweep**: worktrees with no commits in 7 days get auto-archived
  (`git worktree remove`; remote branch left for scott to decide).

## Agent self-check before starting code work

> "Am I about to `commit` / `push` / change source files in a git repo?"
>
> - **Yes** → run `openforge-worktree add <my-agent-id> <task-slug>` first.
> - **No**  → carry on; just don't write into someone else's worktree.

The injected rule in the OpenForge thread context bundle says the same thing
in shorter form. This doc is the long version for humans (and for agents that
want more detail).
