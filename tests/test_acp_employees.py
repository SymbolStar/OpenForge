from __future__ import annotations

import subprocess


def test_acp_allowed_from_openclaw_falls_back_to_empty(fake_home, monkeypatch):
    import forge_employees

    forge_employees._acp_allowed_cache = None

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], output="Config path not found")

    monkeypatch.setattr(forge_employees.subprocess, "check_output", fail)

    assert forge_employees._acp_allowed_from_openclaw() == frozenset()
    assert forge_employees.acp_employee_ids() == frozenset()


def test_acp_employee_ids_intersects_openforge_opt_in(fake_home, monkeypatch):
    import forge_employees

    forge_employees._acp_allowed_cache = None

    calls = []

    def fake_check_output(argv, text, timeout):
        calls.append((argv, text, timeout))
        return '["codex", "claude", "main", "unknown"]'

    monkeypatch.setattr(forge_employees.subprocess, "check_output", fake_check_output)

    assert forge_employees.acp_employee_ids() == frozenset({"codex", "claude"})
    assert forge_employees.acp_employee_ids() == frozenset({"codex", "claude"})
    assert len(calls) == 1
    assert calls[0][0][-3:] == ["get", "acp.allowedAgents", "--json"]
    assert calls[0][1] is True
    assert calls[0][2] == 5


def test_list_and_is_employee_include_configured_acp_runtime(fake_home, monkeypatch):
    import forge_employees

    oc = fake_home / ".openclaw"
    oc.mkdir()
    (oc / "agents" / "codex").mkdir(parents=True)
    (oc / "agents" / "main").mkdir(parents=True)
    monkeypatch.setattr(forge_employees, "_acp_allowed_from_openclaw", lambda: frozenset({"codex", "main"}))

    assert forge_employees.acp_employee_ids() == frozenset({"codex"})
    assert forge_employees.list_employees() == ["codex"]
    assert forge_employees.is_employee("codex") is True
    assert forge_employees.is_employee("main") is False
