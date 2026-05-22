"""Tests for agent_runtime: snapshot/restore, output cleaning, and the
call_agent shell-out (using the fake_openclaw fixture)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def _agent_sessions_dir(home: Path, agent: str) -> Path:
    d = home / ".openclaw" / "agents" / agent / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def ar(fake_home, monkeypatch):
    """agent_runtime with AGENTS_ROOT pointed at the fake $HOME."""
    import agent_runtime as _ar
    monkeypatch.setattr(_ar, "AGENTS_ROOT", fake_home / ".openclaw" / "agents")
    return _ar


# ─── output cleaning ─────────────────────────────────────────────────
def test_clean_strips_noise(ar):
    text = (
        "[plugins] foo loaded\n"
        "Config warnings:\n"
        "- plugins.bar broke\n"
        "🦞 OpenClaw banner\n"
        "actual reply\n\n\n\nmore content"
    )
    out = ar.clean(text)
    assert "actual reply" in out
    assert "[plugins]" not in out
    assert "🦞" not in out
    assert "\n\n\n" not in out


def test_is_empty(ar):
    assert ar.is_empty("") is True
    assert ar.is_empty("   ") is True
    assert ar.is_empty("completed") is True
    assert ar.is_empty("COMPLETED") is True
    assert ar.is_empty("_(空回复)_") is True
    assert ar.is_empty("real content") is False


# ─── session helpers ─────────────────────────────────────────────────
def test_is_forge_sid(ar):
    assert ar._is_forge_sid("forge-th_xx-milk")
    assert ar._is_forge_sid("standup-2026-01-01")
    assert ar._is_forge_sid("huddle-foo")
    assert not ar._is_forge_sid("clean-sid")
    assert not ar._is_forge_sid("")
    assert not ar._is_forge_sid(None)


def test_snapshot_missing_sessions_file(ar):
    assert ar.snapshot_main("nobody") is None


def test_snapshot_polluted_main_recovers_from_disk(ar, fake_home):
    sess = _agent_sessions_dir(fake_home, "milk")
    (sess / "sessions.json").write_text(json.dumps({
        "agent:milk:main": {
            "sessionId": "forge-th_abc-milk",
            "sessionFile": "/tmp/x.jsonl",
        }
    }))
    clean = sess / "clean-abc.jsonl"
    clean.write_text("")
    (sess / "forge-th_xx-milk.jsonl").write_text("")
    snap = ar.snapshot_main("milk")
    assert snap is not None
    assert snap["sessionId"] == "clean-abc"
    assert snap["recoveredFromDisk"]


def test_snapshot_polluted_no_clean_returns_none(ar, fake_home):
    sess = _agent_sessions_dir(fake_home, "milk")
    (sess / "sessions.json").write_text(json.dumps({
        "agent:milk:main": {"sessionId": "forge-x", "sessionFile": "/tmp/x"}
    }))
    # only a forge-* on disk
    (sess / "forge-x.jsonl").write_text("")
    assert ar.snapshot_main("milk") is None


def test_snapshot_main_missing_key_falls_back_to_disk(ar, fake_home):
    sess = _agent_sessions_dir(fake_home, "milk")
    (sess / "sessions.json").write_text(json.dumps({"agent:other:main": {}}))
    (sess / "clean-z.jsonl").write_text("")
    snap = ar.snapshot_main("milk")
    assert snap and snap["sessionId"] == "clean-z"


def test_snapshot_corrupt_sessions_json(ar, fake_home):
    sess = _agent_sessions_dir(fake_home, "milk")
    (sess / "sessions.json").write_text("not json")
    assert ar.snapshot_main("milk") is None


def test_snapshot_clean_main_returns_pointer(ar, fake_home):
    sess = _agent_sessions_dir(fake_home, "milk")
    (sess / "sessions.json").write_text(json.dumps({
        "agent:milk:main": {"sessionId": "clean-a", "sessionFile": "/tmp/y.jsonl"}
    }))
    snap = ar.snapshot_main("milk")
    assert snap and snap["sessionId"] == "clean-a"


def test_find_clean_main_ignores_trajectory(ar, fake_home):
    sess = _agent_sessions_dir(fake_home, "milk")
    (sess / "clean-a.trajectory.jsonl").write_text("")
    (sess / "clean-b.jsonl").write_text("")
    snap = ar._find_clean_main("milk")
    assert snap["sessionId"] == "clean-b"


def test_restore_main_round_trip(ar, fake_home):
    sess = _agent_sessions_dir(fake_home, "milk")
    sessions_json = sess / "sessions.json"
    sessions_json.write_text(json.dumps({
        "agent:milk:main": {
            "sessionId": "forge-th_xx-milk",
            "sessionFile": "/tmp/forge.jsonl",
        }
    }))
    snap = {"agent": "milk", "sessionId": "clean-sid", "sessionFile": "/tmp/y.jsonl"}
    assert ar.restore_main("milk", snap) is True
    d = json.loads(sessions_json.read_text())
    assert d["agent:milk:main"]["sessionId"] == "clean-sid"
    assert d["agent:milk:main"]["restoredFromOpenForge"] is True

    # already clean → refuse to overwrite
    assert ar.restore_main("milk", snap) is False
    # restoring with a forge sid is refused
    assert ar.restore_main("milk", {"sessionId": "forge-xx",
                                    "sessionFile": "/tmp/f"}) is False
    # missing snapshot data
    assert ar.restore_main("milk", {}) is False
    # missing file
    assert ar.restore_main("ghost", snap) is False


def test_restore_main_corrupt_sessions_json(ar, fake_home):
    sess = _agent_sessions_dir(fake_home, "milk")
    (sess / "sessions.json").write_text("not json")
    assert ar.restore_main("milk", {"sessionId": "x", "sessionFile": "/y"}) is False


def test_resolve_openclaw_bin_override(ar, monkeypatch, tmp_path):
    fake = tmp_path / "fake_oc"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("OPENFORGE_OPENCLAW_BIN", str(fake))
    assert ar._resolve_openclaw_bin() == str(fake)


def test_resolve_openclaw_bin_falls_back_to_path(ar, monkeypatch):
    monkeypatch.delenv("OPENFORGE_OPENCLAW_BIN", raising=False)
    # Point HOME at a place with no .nvm so we hit the final return.
    monkeypatch.setenv("HOME", "/tmp/_definitely_no_nvm_here_xyz")
    # _resolve uses Path.home() at call time
    assert ar._resolve_openclaw_bin() == "openclaw"


# ─── call_agent against shell scripts ────────────────────────────────
def test_call_agent_happy_path(ar, monkeypatch):
    monkeypatch.setattr(ar, "OPENCLAW_BIN", ar._resolve_openclaw_bin())
    out = ar.call_agent("milk", "forge-th_test-milk", "hi")
    assert "[mock milk] reply" in out


def test_call_agent_binary_missing(ar, monkeypatch):
    monkeypatch.setattr(ar, "OPENCLAW_BIN", "/no/such/binary/openclaw")
    with pytest.raises(ar.AgentError, match="not found"):
        ar.call_agent("milk", "sid", "hi")


def test_call_agent_nonzero_exit(ar, monkeypatch, tmp_path):
    bad = tmp_path / "bad_openclaw.sh"
    bad.write_text("#!/bin/sh\necho oops 1>&2\nexit 7\n")
    bad.chmod(0o755)
    monkeypatch.setattr(ar, "OPENCLAW_BIN", str(bad))
    with pytest.raises(ar.AgentError, match="exited 7"):
        ar.call_agent("milk", "sid", "hi")


def test_call_agent_empty_output(ar, monkeypatch, tmp_path):
    empty = tmp_path / "empty.sh"
    empty.write_text("#!/bin/sh\nexit 0\n")
    empty.chmod(0o755)
    monkeypatch.setattr(ar, "OPENCLAW_BIN", str(empty))
    with pytest.raises(ar.AgentError, match="no output"):
        ar.call_agent("milk", "sid", "hi")


def test_call_agent_non_json_output_returned_clean(ar, monkeypatch, tmp_path):
    txt = tmp_path / "txt.sh"
    txt.write_text("#!/bin/sh\nprintf 'plain hello\\n'\n")
    txt.chmod(0o755)
    monkeypatch.setattr(ar, "OPENCLAW_BIN", str(txt))
    out = ar.call_agent("milk", "sid", "hi")
    assert out == "plain hello"


def test_call_agent_timeout(ar, monkeypatch, tmp_path):
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    monkeypatch.setattr(ar, "OPENCLAW_BIN", str(slow))
    monkeypatch.setattr(ar, "AGENT_TIMEOUT", 0)
    real_run = subprocess.run

    def _fast_run(*a, **kw):
        kw["timeout"] = 0.2
        return real_run(*a, **kw)
    monkeypatch.setattr(ar.subprocess, "run", _fast_run)
    with pytest.raises(ar.AgentError, match="timeout"):
        ar.call_agent("milk", "sid", "hi")
