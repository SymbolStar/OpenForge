"""PR-3: GET /api/config endpoint (PRD-v1.0 §4.3).

Front-end pulls this at boot to learn the agent webchat base URL,
which is used to turn employee avatars into deep-links to
`<base>/chat?session=agent:<id>:main`. Defaults to loopback; can be
overridden with the OPENFORGE_WEBCHAT_URL env var (useful for Tailscale
or non-default ports).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import _free_port, _wait_up  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _spawn_server(fake_home, env_extra=None):
    port = _free_port()
    env = {**os.environ, "HOME": str(fake_home)}
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, "-u", str(REPO_ROOT / "server.py"), "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_up(f"{base}/api/squads")
    except Exception:
        proc.kill()
        raise
    return proc, base


def _stop(proc):
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as r:
        assert r.status == 200, r.status
        return json.loads(r.read().decode("utf-8"))


# ─── default value ───────────────────────────────────────────────────
def test_config_default_webchat_base(fake_home):
    """No env override → returns the hardcoded loopback default."""
    proc, base = _spawn_server(fake_home)
    try:
        cfg = _get_json(f"{base}/api/config")
        assert cfg["webchat_base_url"] == "http://127.0.0.1:18789"
    finally:
        _stop(proc)


# ─── env override ────────────────────────────────────────────────────
def test_config_env_override(fake_home):
    """OPENFORGE_WEBCHAT_URL → reflected in the response."""
    proc, base = _spawn_server(
        fake_home,
        env_extra={"OPENFORGE_WEBCHAT_URL": "http://example.com:9999"},
    )
    try:
        cfg = _get_json(f"{base}/api/config")
        assert cfg["webchat_base_url"] == "http://example.com:9999"
    finally:
        _stop(proc)


def test_config_strips_trailing_slash(fake_home):
    """Front-end appends `/chat?...` so trailing slashes would cause `//`.
    Server normalises by stripping a trailing slash."""
    proc, base = _spawn_server(
        fake_home,
        env_extra={"OPENFORGE_WEBCHAT_URL": "https://chat.example.com/"},
    )
    try:
        cfg = _get_json(f"{base}/api/config")
        assert cfg["webchat_base_url"] == "https://chat.example.com"
    finally:
        _stop(proc)


# ─── empty env falls back to default (no surprise empty URL) ──────────
def test_config_empty_env_uses_default(fake_home):
    proc, base = _spawn_server(
        fake_home, env_extra={"OPENFORGE_WEBCHAT_URL": "   "}
    )
    try:
        cfg = _get_json(f"{base}/api/config")
        assert cfg["webchat_base_url"] == "http://127.0.0.1:18789"
    finally:
        _stop(proc)


# ─── unit-level: forge_config.get_config() shape ─────────────────────
def test_get_config_unit(monkeypatch):
    """Module-level smoke: get_config() returns a dict with the expected
    key, and respects the env at call time."""
    import importlib

    import forge_config
    importlib.reload(forge_config)
    monkeypatch.setenv("OPENFORGE_WEBCHAT_URL", "http://unit.test:1")
    cfg = forge_config.get_config()
    assert isinstance(cfg, dict)
    assert cfg["webchat_base_url"] == "http://unit.test:1"

    monkeypatch.delenv("OPENFORGE_WEBCHAT_URL", raising=False)
    cfg2 = forge_config.get_config()
    assert cfg2["webchat_base_url"] == "http://127.0.0.1:18789"
