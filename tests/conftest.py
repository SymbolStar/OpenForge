"""Shared pytest fixtures for OpenForge tests.

Every test gets a fresh fake $HOME so forge_store's ~/.openforge/ tree is
empty + isolated. We also stub OPENFORGE_OPENCLAW_BIN so post_router
never tries to shell out to a real openclaw binary.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Make sure subprocesses (test_server boots a real server.py) inherit the
# coverage subprocess hook. We unconditionally prepend REPO_ROOT to PYTHONPATH
# so sitecustomize.py is importable; the hook itself only activates when
# COVERAGE_PROCESS_START is set in the environment.
_existing_pp = os.environ.get("PYTHONPATH", "")
_pp_parts = [str(REPO_ROOT)] + ([_existing_pp] if _existing_pp else [])
os.environ["PYTHONPATH"] = os.pathsep.join(_pp_parts)


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Point $HOME at tmp_path and force every forge module to re-resolve."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Clear PR-A OPENFORGE_DIR override if the test environment inherited it
    # (e.g. dev shell). Tests must always resolve to fake $HOME.
    monkeypatch.delenv("OPENFORGE_DIR", raising=False)
    # And the new OPENFORGE_HOME override — same reasoning.
    monkeypatch.delenv("OPENFORGE_HOME", raising=False)
    monkeypatch.setenv("OPENFORGE_OPENCLAW_BIN", str(REPO_ROOT / "tests" / "fixtures" / "fake_openclaw.sh"))
    # Force reload so module-level Path.home() captures the new HOME.
    for name in [
        "forge_paths",
        "forge_store",
        "forge_refs",
        "forge_context",
        "forge_employees",
        "forge_identity",
        "forge_session_search",
        "forge_uploads",
        "agent_runtime",
        "post_router",
    ]:
        if name in sys.modules:
            del sys.modules[name]
    return home


@pytest.fixture
def store(fake_home):
    import forge_store
    return forge_store


@pytest.fixture
def router(fake_home):
    import post_router
    return post_router


# ─── shared subprocess-server fixture for HTTP tests ─────────────────

def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_up(url: str, timeout: float = 8.0) -> None:
    import time
    import urllib.error
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return
        except (urllib.error.URLError, ConnectionResetError, ConnectionRefusedError):
            time.sleep(0.1)
    raise RuntimeError(f"server did not come up at {url}")


@pytest.fixture
def server(fake_home):
    import signal
    import subprocess
    import sys
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-u", str(REPO_ROOT / "server.py"), "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "HOME": str(fake_home)},
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_up(f"{base}/api/squads")
        yield base
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
