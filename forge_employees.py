"""Employee discovery for OpenForge.

An "employee" is an agent that has:
  1. A curated identity (~/.openclaw/workspace-<id>/SOUL.md), AND
  2. An actual agent runtime presence (~/.openclaw/agents/<id>/).

Both conditions are required. Either alone is a false positive:
  - Some workspace-* dirs are OpenClaw *project repos* (e.g. workspace-
    clawdesign), not agent workspaces. They ship with template SOUL.md
    files but represent products, not staff. Their actual agent lives
    elsewhere (clawdesign uses agent `designer`).
  - Some agents/<id> dirs are runtime / LLM-CLI profiles (codex, claude-
    code, claude, main) with no curated identity — not real employees.

Distinguished from /api/agents, which surfaces ANY discoverable agent for
the @-picker (including runtime profiles).

Public API:
    list_employees() -> list[str]   # sorted agent ids
    is_employee(agent_id) -> bool
    EMPLOYEE_WORKSPACE_PREFIX        # for tests and introspection
"""
from __future__ import annotations

from pathlib import Path

EMPLOYEE_WORKSPACE_PREFIX = "workspace-"
EMPLOYEE_MARKER = "SOUL.md"
AGENTS_DIRNAME = "agents"


def _openclaw_root() -> Path:
    return Path.home() / ".openclaw"


def _has_agent_runtime(root: Path, agent_id: str) -> bool:
    """True iff ~/.openclaw/agents/<agent_id>/ exists as a dir."""
    agent_dir = root / AGENTS_DIRNAME / agent_id
    return agent_dir.exists() and agent_dir.is_dir()


def list_employees() -> list[str]:
    """Return sorted list of agent ids that are real employees.

    Requires BOTH:
      - ~/.openclaw/workspace-<id>/SOUL.md exists (curated identity), AND
      - ~/.openclaw/agents/<id>/ exists (actual agent runtime)

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
        if not (marker.exists() and marker.is_file()):
            continue
        if not _has_agent_runtime(root, agent_id):
            # workspace-* without a matching agents/<id>/ is a project repo,
            # not an employee (e.g. workspace-clawdesign -> agent `designer`).
            continue
        out.append(agent_id)
    out.sort()
    return out


def is_employee(agent_id: str) -> bool:
    """True iff agent has BOTH a workspace-<id>/SOUL.md and agents/<id>/."""
    if not agent_id or not isinstance(agent_id, str):
        return False
    if "/" in agent_id or ".." in agent_id:
        return False
    root = _openclaw_root()
    marker = root / f"{EMPLOYEE_WORKSPACE_PREFIX}{agent_id}" / EMPLOYEE_MARKER
    if not (marker.exists() and marker.is_file()):
        return False
    return _has_agent_runtime(root, agent_id)
