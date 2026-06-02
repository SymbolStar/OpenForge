"""forge_paths.py — single source of truth for OpenForge runtime paths.

Before this module, every store/file/refs/uploads module had its own
hard-coded `Path.home() / ".openclaw" / "openforge"`. That hard-coding
meant:

  1. OpenForge data was wedged inside OpenClaw's home dir even though
     OpenForge is its own product.
  2. There was no single knob to relocate state (testing, multi-user
     scenarios, "I just lost my disk and want to put $HOME on a new
     volume" — all required grepping the codebase).

Design (mirrors OpenClaw's own ~/.openclaw convention):

  ~/.openforge/                      ← OpenForge's home (this module)
    ├── config.json                  ← file roots, default project root
    ├── squads.json                  ← squad definitions
    ├── threads/<thread-id>/         ← per-thread event log
    ├── refs.jsonl                   ← cross-thread ref index
    ├── favorites.jsonl              ← file favorites
    ├── files/                       ← built-in writable file root
    ├── uploads/                     ← image/file uploads (sha-addressed)
    └── context_bundles/             ← per-agent context cache

OpenClaw-owned paths we still read (kept on ~/.openclaw/ on purpose —
those are OpenClaw's territory, OpenForge is a guest):

  ~/.openclaw/agents/<id>/sessions/  ← agent session transcripts
  ~/.openclaw/workspace-<agent>/     ← per-agent workspace
    └── openforge-uploads/           ← carved by OpenForge into the
                                       agent workspace so the agent's
                                       own tooling can find uploaded
                                       files; intentional coupling.

Override knob:
  OPENFORGE_HOME=/some/path           # relocate everything above

Migration:
  On first import, if ~/.openforge/ does NOT exist but
  ~/.openclaw/openforge/ DOES, we move it in one shot and leave a
  README pointer behind so older releases (or curious operators) can
  find their data. This keeps "git pull && restart" upgrade-safe for
  existing installs without forcing a manual migration step.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

__all__ = [
    "openforge_home",
    "config_path",
    "squads_path",
    "threads_dir",
    "refs_path",
    "favorites_path",
    "files_dir",
    "uploads_dir",
    "context_bundles_dir",
    "openclaw_home",
    "openclaw_agents_root",
    "openclaw_workspace_dir",
    "migrate_legacy_home_if_needed",
]


def _home() -> Path:
    """Return $HOME (test-friendly indirection — monkeypatch this in tests)."""
    return Path.home()


def openforge_home() -> Path:
    """Root for all OpenForge-owned state.

    Honors OPENFORGE_HOME env var; defaults to ~/.openforge. The dir is
    NOT created here — let writers mkdir as they need (matches how the
    old hard-coded paths behaved).
    """
    override = os.environ.get("OPENFORGE_HOME")
    if override:
        return Path(override).expanduser()
    return _home() / ".openforge"


# ─── OpenForge-owned files/dirs ──────────────────────────────────────


def config_path() -> Path:
    return openforge_home() / "config.json"


def squads_path() -> Path:
    return openforge_home() / "squads.json"


def threads_dir() -> Path:
    return openforge_home() / "threads"


def refs_path() -> Path:
    return openforge_home() / "refs.jsonl"


def favorites_path() -> Path:
    return openforge_home() / "favorites.jsonl"


def files_dir() -> Path:
    return openforge_home() / "files"


def uploads_dir() -> Path:
    return openforge_home() / "uploads"


def context_bundles_dir() -> Path:
    return openforge_home() / "context_bundles"


# ─── OpenClaw-owned, read by OpenForge ───────────────────────────────
# These stay under ~/.openclaw/ on purpose: they belong to OpenClaw,
# OpenForge merely reads them. If OpenClaw ever offers its own override
# (OPENCLAW_HOME or similar) we can add it here without touching call
# sites.


def openclaw_home() -> Path:
    return _home() / ".openclaw"


def openclaw_agents_root() -> Path:
    return openclaw_home() / "agents"


def openclaw_workspace_dir(agent_id: str) -> Path:
    """Per-agent workspace owned by OpenClaw.

    Used by uploads (we carve `openforge-uploads/` inside) and by
    `forge_employees` to find IDENTITY.md / SOUL.md.
    """
    return openclaw_home() / f"workspace-{agent_id}"


# ─── one-shot migration from legacy ~/.openclaw/openforge ────────────


_LEGACY_README = """\
This directory used to be OpenForge's state home.

Starting with the "OpenForge runtime home" refactor, OpenForge keeps
its state under ~/.openforge/ (override with $OPENFORGE_HOME). Your
data was moved there automatically on the first server start after the
upgrade.

If you want to roll back, stop the server and `mv` ~/.openforge back
to this path, then downgrade.
"""


def migrate_legacy_home_if_needed() -> Path | None:
    """If ~/.openclaw/openforge exists and ~/.openforge does not, move
    the former to the latter. Idempotent: returns the new path on first
    migration, None thereafter.

    We intentionally do NOT migrate when OPENFORGE_HOME is set to a
    custom location — that's an explicit operator choice and we don't
    want to silently merge legacy state into it.
    """
    if os.environ.get("OPENFORGE_HOME"):
        return None

    new_home = openforge_home()
    legacy = openclaw_home() / "openforge"

    if new_home.exists():
        return None
    if not legacy.exists():
        return None

    # Move (rename if same filesystem, copy+delete otherwise).
    new_home.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy), str(new_home))

    # Leave a breadcrumb so the next operator who greps for
    # ~/.openclaw/openforge in old docs/scripts can find their data.
    try:
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "MOVED.md").write_text(_LEGACY_README, encoding="utf-8")
    except OSError:
        # Best-effort breadcrumb — failure here is not fatal.
        pass

    return new_home
