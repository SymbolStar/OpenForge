"""Shared pytest fixtures for OpenForge tests.

Every test gets a fresh fake $HOME so forge_store's ~/.openclaw/openforge/
tree is empty + isolated. We also stub OPENFORGE_OPENCLAW_BIN so post_router
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


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Point $HOME at tmp_path and force every forge module to re-resolve."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENFORGE_OPENCLAW_BIN", str(REPO_ROOT / "tests" / "fixtures" / "fake_openclaw.sh"))
    # Force reload so module-level Path.home() captures the new HOME.
    for name in [
        "forge_store",
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
