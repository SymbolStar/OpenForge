from __future__ import annotations

import subprocess

import pytest


class _FakePopen:
    """Drop-in replacement for subprocess.Popen used by acp_runtime tests.

    The real implementation runs in its own process group so the cancel
    endpoint (PR feature/interrupt-dispatch) can SIGTERM it. We only need
    to fake `communicate()` + `returncode` + `pid` here.
    """

    def __init__(self, argv, cwd=None, env=None, stdout=None, stderr=None,
                 text=False, start_new_session=False, _result=None, _raise=None):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.text = text
        self._result = _result or ("", "", 0)
        self._raise = _raise
        self.pid = 999_999
        self.returncode = 0

    def communicate(self, timeout=None):
        if self._raise:
            raise self._raise
        out, err, rc = self._result
        self.returncode = rc
        return out, err

    def wait(self, timeout=None):
        return self.returncode


def test_call_acp_agent_success(monkeypatch):
    import acp_runtime

    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return _FakePopen(argv, _result=(" reply \n", "", 0), **{k: v for k, v in kwargs.items() if k in ("cwd", "env", "text", "start_new_session")}, stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"))

    monkeypatch.setattr(acp_runtime.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(acp_runtime, "ACP_AGENT_TIMEOUT", 9)

    out = acp_runtime.call_acp_agent(
        "codex", "sid", "do it", extra_env={"OPENFORGE_PROJECT_DIR": "/tmp/project"}
    )

    assert out == "reply"
    argv, kwargs = calls[0]
    assert argv == [
        "codex", "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "do it",
    ]
    assert kwargs["cwd"] == "/tmp/project"
    assert kwargs["env"]["OPENFORGE_PROJECT_DIR"] == "/tmp/project"
    assert kwargs["text"] is True
    assert kwargs["start_new_session"] is True


def test_call_acp_agent_timeout(monkeypatch):
    import acp_runtime
    from agent_runtime import AgentError

    def fake_popen(argv, **kwargs):
        return _FakePopen(argv, _raise=subprocess.TimeoutExpired(argv[0], timeout=1))

    monkeypatch.setattr(acp_runtime.subprocess, "Popen", fake_popen)
    # Avoid SIGTERM on bogus pid.
    monkeypatch.setattr(acp_runtime, "_killpg_safe", lambda pgid, sig: None)
    monkeypatch.setattr(acp_runtime, "ACP_AGENT_TIMEOUT", 1)

    with pytest.raises(AgentError, match="timeout after 1s"):
        acp_runtime.call_acp_agent("codex", "sid", "hi")


def test_call_acp_agent_failure(monkeypatch):
    import acp_runtime
    from agent_runtime import AgentError

    def fake_popen(argv, **kwargs):
        return _FakePopen(argv, _result=("", "bad\nfailed", 7))

    monkeypatch.setattr(acp_runtime.subprocess, "Popen", fake_popen)

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
