# Contributing to OpenForge

Aimed at agents (and humans) opening PRs against `SymbolStar/OpenForge`.

## Quickstart

```bash
git clone git@github.com:SymbolStar/OpenForge.git
cd OpenForge

# one-time local dev env
python3 -m venv .venv
.venv/bin/pip install pytest ruff

# run the gates locally before pushing
.venv/bin/ruff check .
.venv/bin/python -m pytest tests/ -v

# install the service (one-time per machine)
./bin/forge install
forge open
```

## CI gates (all must pass on PR before merge)

| Job          | What it checks                                                    |
|--------------|-------------------------------------------------------------------|
| `lint`       | `ruff check .` — no unused imports, no obvious bugs, no bad imports |
| `shellcheck` | `bin/forge` + `tests/fixtures/*.sh` parse cleanly                 |
| `test`       | `pytest tests/` on Python 3.11 / 3.12 / 3.13                      |
| `smoke`      | Boots `server.py`, exercises the HTTP API end-to-end via curl     |

## Commit style

Conventional commits:

```
feat(router):  ...
fix(server):   ...
chore(ci):     ...
docs(prd):     ...
test(store):   ...
refactor:      ...
polish(ui):    ...
```

The `release.yml` workflow **skips releases** for commits that match
`^(docs|chore|test|ci)(\(|:)` — pure doc / chore / test commits do not
move user-visible behavior, so they don't get a tag of their own.

## PR rules

1. **Keep PRs small** — under ~300 LOC when possible. One concern per PR.
2. **Tests are mandatory** for new behavior. Bug fixes should land
   with a regression test that fails before the fix.
3. **No PR without a green CI run** — the branch protection rule will
   block merge.
4. **One reviewer**, agent or human. Reviewer's job is to confirm the
   contract (events, API, projection) hasn't silently changed.

## What CAN you change without a PR?

- `memory/` files in your own workspace — these are agent-local notes,
  not part of the product.
- Your own `~/.openclaw/openforge/` data dir — never check this in.

## What MUST go through a PR?

- Anything under `forge_store.py`, `post_router.py`, `agent_runtime.py`,
  `server.py`, `web/`, `bin/`, `tests/`, `.github/`, `docs/`.

## Releases

Auto-tagged + GitHub Release on every green non-doc commit to `main`:

```
v2026.05.21
v2026.05.21-abc1234   (when same day already has a tag)
```

The release notes are auto-generated from the merged PRs since the
previous tag.

## Need to update OpenForge running on your machine?

```bash
forge update         # interactive: pulls + restarts
forge selfupdate     # cron-friendly: silent on no-op, restarts only on change
forge logs --follow  # tail the live log
forge status         # see what's running + URL
```
