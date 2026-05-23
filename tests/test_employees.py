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

def _make_workspace(home: Path, agent_id: str, *, with_soul: bool = True) -> None:
    ws = home / f"workspace-{agent_id}"
    ws.mkdir(parents=True, exist_ok=True)
    if with_soul:
        (ws / "SOUL.md").write_text(f"# SOUL — {agent_id}\n")


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


def test_list_employees_excludes_agents_dir_profiles(fake_home):
    """codex / claude-code / main live in agents/, never in workspace-*. They
    must not appear in the employee roster."""
    import forge_employees
    oc = fake_home / ".openclaw"
    oc.mkdir()
    agents = oc / "agents"
    agents.mkdir()
    for runtime in ("codex", "claude-code", "main", "claude"):
        (agents / runtime / "sessions").mkdir(parents=True)
    _make_workspace(oc, "alice")
    _make_workspace(oc, "designer")  # Dora
    assert forge_employees.list_employees() == ["alice", "designer"]


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


def test_http_employees_excludes_runtime_profiles(fake_home, server):
    """Regression for the 'Dora missing from members' bug. Even with codex
    et al. living in agents/, the endpoint must only return workspace-* owners."""
    oc = fake_home / ".openclaw"
    oc.mkdir(exist_ok=True)
    agents = oc / "agents"
    agents.mkdir()
    for runtime in ("codex", "claude-code", "main"):
        (agents / runtime / "sessions").mkdir(parents=True)
    _make_workspace(oc, "designer")

    code, body = _get(f"{server}/api/employees")
    assert code == 200
    assert body == ["designer"]
    for runtime in ("codex", "claude-code", "main"):
        assert runtime not in body


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
