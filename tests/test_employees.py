"""Unit + HTTP tests for the employee discovery module.

Employees = agents with `~/.openclaw/workspace-<id>/SOUL.md`. This module
is the single source of truth for "who counts as a real employee", used
by the squad member-picker. No hardcoded blocklist anywhere.
"""
# ruff: noqa: F811
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import server  # noqa: F401, E402

# ─── module-level unit tests ────────────────────────────────────────

def _make_workspace(home: Path, agent_id: str, *, with_soul: bool = True, with_agent: bool = True) -> None:
    """Create a workspace and (by default) a matching agents/<id>/ dir.

    A real employee needs BOTH workspace-<id>/SOUL.md AND agents/<id>/.
    Tests can pass with_agent=False to simulate workspace-only project repos
    like workspace-clawdesign.
    """
    ws = home / f"workspace-{agent_id}"
    ws.mkdir(parents=True, exist_ok=True)
    if with_soul:
        (ws / "SOUL.md").write_text(f"# SOUL — {agent_id}\n")
    if with_agent:
        (home / "agents" / agent_id).mkdir(parents=True, exist_ok=True)


def test_list_employees_empty_on_fresh_home(fake_home):
    import forge_employees
    # fake_home has no .openclaw yet
    assert forge_employees.list_employees() == []


def test_list_employees_finds_workspace_with_soul(fake_home):
    import forge_employees
    oc = fake_home / ".openclaw"
    oc.mkdir()
    _make_workspace(oc, "alice")
    _make_workspace(oc, "dora")
    _make_workspace(oc, "judy")
    assert forge_employees.list_employees() == ["alice", "dora", "judy"]


def test_list_employees_skips_workspace_without_agent_dir(fake_home):
    """Regression for the clawdesign bug. workspace-clawdesign has a SOUL.md
    (it's a complete OpenClaw project template) but no agents/clawdesign/
    — the actual agent is `designer`. The workspace is a *project repo*,
    not an employee."""
    import forge_employees
    oc = fake_home / ".openclaw"
    oc.mkdir()
    _make_workspace(oc, "alice")
    _make_workspace(oc, "clawdesign", with_agent=False)
    _make_workspace(oc, "designer")
    assert forge_employees.list_employees() == ["alice", "designer"]


def test_list_employees_skips_workspace_without_soul(fake_home):
    import forge_employees
    oc = fake_home / ".openclaw"
    oc.mkdir()
    _make_workspace(oc, "alice", with_soul=True)
    _make_workspace(oc, "stub", with_soul=False)
    assert forge_employees.list_employees() == ["alice"]


def test_list_employees_ignores_non_workspace_dirs(fake_home):
    import forge_employees
    oc = fake_home / ".openclaw"
    oc.mkdir()
    (oc / "agents").mkdir()
    (oc / "openforge").mkdir()
    _make_workspace(oc, "alice")
    assert forge_employees.list_employees() == ["alice"]


def test_list_employees_ignores_empty_suffix(fake_home):
    """`workspace-` (no id) shouldn't be picked up as employee ''."""
    import forge_employees
    oc = fake_home / ".openclaw"
    oc.mkdir()
    (oc / "workspace-").mkdir()
    (oc / "workspace-").joinpath("SOUL.md").write_text("x")
    _make_workspace(oc, "alice")
    assert forge_employees.list_employees() == ["alice"]


def test_list_employees_includes_allowlisted_runtime_agents(fake_home):
    """codex / claude-code count as employees (squad-addable LLM workers)
    even without a workspace-<id>/SOUL.md. main / claude stay out."""
    import forge_employees
    oc = fake_home / ".openclaw"
    oc.mkdir()
    agents = oc / "agents"
    agents.mkdir()
    for runtime in ("codex", "claude-code", "main", "claude"):
        (agents / runtime / "sessions").mkdir(parents=True)
    _make_workspace(oc, "alice")
    _make_workspace(oc, "designer")  # Dora
    assert forge_employees.list_employees() == [
        "alice", "claude-code", "codex", "designer",
    ]


def test_is_employee_positive(fake_home):
    import forge_employees
    oc = fake_home / ".openclaw"
    oc.mkdir()
    _make_workspace(oc, "alice")
    assert forge_employees.is_employee("alice") is True


def test_is_employee_negative(fake_home):
    import forge_employees
    oc = fake_home / ".openclaw"
    oc.mkdir()
    _make_workspace(oc, "alice", with_soul=False)
    assert forge_employees.is_employee("alice") is False
    assert forge_employees.is_employee("ghost") is False


def test_is_employee_requires_agent_dir(fake_home):
    """Workspace + SOUL.md alone (no agents/<id>/) is a project repo, not an employee."""
    import forge_employees
    oc = fake_home / ".openclaw"
    oc.mkdir()
    _make_workspace(oc, "clawdesign", with_agent=False)
    assert forge_employees.is_employee("clawdesign") is False


def test_is_employee_allowlisted_runtime_agent(fake_home):
    """codex / claude-code count as employees once their agents/<id>/ exists,
    even with no workspace-<id>/SOUL.md."""
    import forge_employees
    oc = fake_home / ".openclaw"
    oc.mkdir()
    (oc / "agents" / "codex").mkdir(parents=True)
    (oc / "agents" / "claude-code").mkdir(parents=True)
    assert forge_employees.is_employee("codex") is True
    assert forge_employees.is_employee("claude-code") is True
    # main / claude are NOT on the allowlist even if their dirs exist
    (oc / "agents" / "main").mkdir(parents=True)
    assert forge_employees.is_employee("main") is False
    # And the allowlist doesn't bypass the agents-dir requirement
    assert forge_employees.is_employee("codex") is True
    import shutil
    shutil.rmtree(oc / "agents" / "codex")
    assert forge_employees.is_employee("codex") is False


@pytest.mark.parametrize("bad", ["", "../passwd", "a/b", None])
def test_is_employee_rejects_invalid_ids(fake_home, bad):
    import forge_employees
    assert forge_employees.is_employee(bad) is False


# ─── HTTP-level tests ───────────────────────────────────────────────

def _get(url: str) -> tuple[int, object]:
    with urllib.request.urlopen(url, timeout=2) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_http_employees_endpoint_returns_workspace_owners(fake_home, server):
    oc = fake_home / ".openclaw"
    oc.mkdir(exist_ok=True)
    _make_workspace(oc, "alice")
    _make_workspace(oc, "designer")  # Dora — the original bug case
    _make_workspace(oc, "judy")

    code, body = _get(f"{server}/api/employees")
    assert code == 200
    assert body == ["alice", "designer", "judy"]


def test_http_employees_excludes_project_repos(fake_home, server):
    """Regression for the clawdesign-in-members bug."""
    oc = fake_home / ".openclaw"
    oc.mkdir(exist_ok=True)
    _make_workspace(oc, "alice")
    _make_workspace(oc, "clawdesign", with_agent=False)
    _make_workspace(oc, "designer")

    code, body = _get(f"{server}/api/employees")
    assert code == 200
    assert body == ["alice", "designer"]
    assert "clawdesign" not in body


def test_http_employees_includes_allowlisted_runtime_agents(fake_home, server):
    """codex / claude-code are squad-addable LLM workers — they must appear
    in /api/employees even though they have no workspace-<id>/SOUL.md.
    main and other runtime profiles stay out."""
    oc = fake_home / ".openclaw"
    oc.mkdir(exist_ok=True)
    agents = oc / "agents"
    agents.mkdir()
    for runtime in ("codex", "claude-code", "main"):
        (agents / runtime / "sessions").mkdir(parents=True)
    _make_workspace(oc, "designer")

    code, body = _get(f"{server}/api/employees")
    assert code == 200
    assert body == ["claude-code", "codex", "designer"]
    assert "main" not in body


def test_http_employees_empty_when_no_workspaces(fake_home, server):
    code, body = _get(f"{server}/api/employees")
    assert code == 200
    assert body == []


def test_http_agents_endpoint_still_includes_runtime_profiles(fake_home, server):
    """`/api/agents` is the @-picker source; it intentionally surfaces ANY
    agent dir (incl. codex/claude-code) so users can mention them. Only
    `/api/employees` should filter to curated roster."""
    oc = fake_home / ".openclaw"
    oc.mkdir(exist_ok=True)
    (oc / "agents" / "codex" / "sessions").mkdir(parents=True)
    _make_workspace(oc, "alice")

    code, body = _get(f"{server}/api/agents")
    assert code == 200
    # /api/agents = union(squad members, agents/<id>/sessions); codex must be there.
    assert "codex" in body
