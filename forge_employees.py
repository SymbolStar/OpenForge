"""Employee discovery for OpenForge.

An "employee" is an agent that can be added as a squad member. Two
flavours qualify:

  1. **Curated employees** — the original kind. Need BOTH:
       a. `~/.openclaw/workspace-<id>/SOUL.md` (curated identity), AND
       b. `~/.openclaw/agents/<id>/`            (runtime presence).
     These have hand-written SOUL.md / IDENTITY.md and show up with a
     friendly display name + emoji.

  2. **Runtime LLM-CLI agents** (opt-in allowlist) — currently `codex`
     and `claude-code`. They live only in `~/.openclaw/agents/<id>/`
     with no SOUL.md, but Scott explicitly wants them addable to
     squads as generic LLM workers. Their display name falls back to
     the agent id (the identity layer handles that gracefully).

Everything else is filtered out:
  - `workspace-clawdesign` and friends are *project repos*, not staff;
    their actual agent lives elsewhere (clawdesign → `designer`).
  - `agents/main`, `agents/claude` are infrastructure profiles, not
    user-pickable workers.

The runtime allowlist is intentionally tiny + explicit instead of
"every dir under agents/" because that path also contains main /
claude / and any future plumbing profile we add. New runtimes get
added here by hand after we agree they're squad-eligible.

Distinguished from /api/agents, which surfaces ANY discoverable agent
for the @-picker (a strictly broader set).

Public API:
    list_employees() -> list[str]   # sorted agent ids
    is_employee(agent_id) -> bool
    EMPLOYEE_WORKSPACE_PREFIX        # for tests and introspection
    RUNTIME_EMPLOYEE_IDS             # the opt-in runtime allowlist
"""
from __future__ import annotations

from pathlib import Path

EMPLOYEE_WORKSPACE_PREFIX = "workspace-"
EMPLOYEE_MARKER = "SOUL.md"
AGENTS_DIRNAME = "agents"

# Runtime LLM-CLI agents that qualify as squad members even without a
# workspace-<id>/SOUL.md. Keep this list tight: only generic LLM workers
# the user might plausibly @ in a squad. NEVER include 'main' or
# 'claude' — those are infrastructure.
RUNTIME_EMPLOYEE_IDS: frozenset[str] = frozenset({"codex", "claude-code"})


def _openclaw_root() -> Path:
    import forge_paths
    return forge_paths.openclaw_home()


def _has_agent_runtime(root: Path, agent_id: str) -> bool:
    """True iff ~/.openclaw/agents/<agent_id>/ exists as a dir."""
    agent_dir = root / AGENTS_DIRNAME / agent_id
    return agent_dir.exists() and agent_dir.is_dir()


def list_employees() -> list[str]:
    """Return sorted list of agent ids that qualify as squad members.

    Includes:
      - Curated employees: workspace-<id>/SOUL.md + agents/<id>/ both present.
      - Runtime LLM-CLI agents from RUNTIME_EMPLOYEE_IDS whose
        agents/<id>/ exists (no SOUL.md required).

    Returns [] if ~/.openclaw doesn't exist (fresh install).
    """
    root = _openclaw_root()
    if not root.exists() or not root.is_dir():
        return []
    out: set[str] = set()
    # 1. Curated workspace-<id> employees.
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
        out.add(agent_id)
    # 2. Allowlisted runtime LLM-CLI agents (codex, claude-code, …).
    for runtime_id in RUNTIME_EMPLOYEE_IDS:
        if _has_agent_runtime(root, runtime_id):
            out.add(runtime_id)
    return sorted(out)


def is_employee(agent_id: str) -> bool:
    """True iff `agent_id` qualifies as a squad member.

    Mirrors list_employees(): curated workspace + agents/ OR an
    allowlisted runtime id with agents/<id>/.
    """
    if not agent_id or not isinstance(agent_id, str):
        return False
    if "/" in agent_id or ".." in agent_id:
        return False
    root = _openclaw_root()
    if agent_id in RUNTIME_EMPLOYEE_IDS:
        return _has_agent_runtime(root, agent_id)
    marker = root / f"{EMPLOYEE_WORKSPACE_PREFIX}{agent_id}" / EMPLOYEE_MARKER
    if not (marker.exists() and marker.is_file()):
        return False
    return _has_agent_runtime(root, agent_id)
