"""Employee discovery for OpenForge.

An "employee" is an agent that has a curated identity — i.e. a SOUL.md
file under ~/.openclaw/workspace-<id>/. Distinguished from:

- Runtime / LLM CLI profiles (codex, claude-code, claude, main): live
  under ~/.openclaw/agents/ but have no workspace + no SOUL.md
- Discoverable-but-not-staffed agents: have an agents/ session dir but
  no workspace yet

This module is the single source of truth for "who counts as a real
employee" — used by the squad member picker and any other surface that
needs to filter to the curated roster.

Public API:
    list_employees() -> list[str]   # sorted agent ids
    is_employee(agent_id) -> bool
    EMPLOYEE_WORKSPACE_PREFIX        # for tests and introspection
"""
from __future__ import annotations

from pathlib import Path

EMPLOYEE_WORKSPACE_PREFIX = "workspace-"
EMPLOYEE_MARKER = "SOUL.md"


def _openclaw_root() -> Path:
    return Path.home() / ".openclaw"


def list_employees() -> list[str]:
    """Return sorted list of agent ids that have a curated workspace+SOUL.md.

    A workspace is `~/.openclaw/workspace-<id>/`, and the agent counts as
    an employee iff `workspace-<id>/SOUL.md` exists and is a regular file.

    Returns [] if ~/.openclaw doesn't exist (fresh install).
    """
    root = _openclaw_root()
    if not root.exists() or not root.is_dir():
        return []
    out: list[str] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not name.startswith(EMPLOYEE_WORKSPACE_PREFIX):
            continue
        agent_id = name[len(EMPLOYEE_WORKSPACE_PREFIX):]
        if not agent_id:
            continue
        marker = child / EMPLOYEE_MARKER
        if marker.exists() and marker.is_file():
            out.append(agent_id)
    out.sort()
    return out


def is_employee(agent_id: str) -> bool:
    """True iff `~/.openclaw/workspace-<agent_id>/SOUL.md` exists."""
    if not agent_id or not isinstance(agent_id, str):
        return False
    if "/" in agent_id or ".." in agent_id:
        return False
    marker = _openclaw_root() / f"{EMPLOYEE_WORKSPACE_PREFIX}{agent_id}" / EMPLOYEE_MARKER
    return marker.exists() and marker.is_file()
