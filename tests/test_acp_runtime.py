from __future__ import annotations

import subprocess

import pytest


def test_call_acp_agent_success(monkeypatch):
    import acp_runtime

    calls = []

    def fake_run(argv, cwd, env, capture_output, text, timeout):
        calls.append((argv, cwd, env, capture_output, text, timeout))
        return subprocess.CompletedProcess(argv, 0, stdout=" reply \n", stderr="")

    monkeypatch.setattr(acp_runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(acp_runtime, "ACP_AGENT_TIMEOUT", 9)

    out = acp_runtime.call_acp_agent(
        "codex", "sid", "do it", extra_env={"OPENFORGE_PROJECT_DIR": "/tmp/project"}
    )

    assert out == "reply"
    assert calls[0][0] == [
        "codex", "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "do it",
    ]
    assert calls[0][1] == "/tmp/project"
    assert calls[0][2]["OPENFORGE_PROJECT_DIR"] == "/tmp/project"
    assert calls[0][3] is True
    assert calls[0][4] is True
    assert calls[0][5] == 9


def test_call_acp_agent_timeout(monkeypatch):
    import acp_runtime
    from agent_runtime import AgentError

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=1)

    monkeypatch.setattr(acp_runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(acp_runtime, "ACP_AGENT_TIMEOUT", 1)

    with pytest.raises(AgentError, match="timeout after 1s"):
        acp_runtime.call_acp_agent("codex", "sid", "hi")


def test_call_acp_agent_failure(monkeypatch):
    import acp_runtime
    from agent_runtime import AgentError

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 7, stdout="", stderr="bad\nfailed")

    monkeypatch.setattr(acp_runtime.subprocess, "run", fake_run)

    with pytest.raises(AgentError, match=r"exited 7: bad \| failed"):
        acp_runtime.call_acp_agent("claude", "sid", "hi")


def test_render_acp_preamble_includes_identity_thread_and_trigger(fake_home):
    import post_router

    ws = fake_home / ".openclaw" / "workspace-codex"
    ws.mkdir(parents=True)
    (ws / "SOUL.md").write_text("Codex soul text", encoding="utf-8")

    preamble = post_router._render_acp_preamble(
        "th_123", "codex", {"speaker": "scott", "content": "@codex fix tests"}
    )

    assert "你的身份: codex ACP CLI employee" in preamble
    assert "thread_id: th_123" in preamble
    assert "Codex soul text" in preamble
    assert "[触发 post 原文 — from: scott]" in preamble
    assert "@codex fix tests" in preamble
