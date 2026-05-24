"""Tests for forge_identity — IDENTITY.md parsing + display-name resolution.

Uses the `fake_home` fixture (provided in conftest.py) so we can stand up a
controlled set of workspaces without touching ~/.openclaw.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _make_employee(home: Path, agent_id: str, name: str = "",
                   emoji: str = "") -> None:
    """Create ~/.openclaw/workspace-<id>/{SOUL.md,IDENTITY.md} and the
    matching ~/.openclaw/agents/<id>/ marker so forge_employees picks it
    up. Pass empty strings to skip the corresponding IDENTITY.md line."""
    ws = home / ".openclaw" / f"workspace-{agent_id}"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "SOUL.md").write_text(f"# SOUL of {agent_id}\n", encoding="utf-8")
    lines = []
    if name:
        lines.append(f"- **Name:** {name}")
    if emoji:
        lines.append(f"- **Emoji:** {emoji}")
    (ws / "IDENTITY.md").write_text(
        "# IDENTITY.md\n\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    (home / ".openclaw" / "agents" / agent_id).mkdir(parents=True, exist_ok=True)


@pytest.fixture
def identity(fake_home, monkeypatch):
    """Reload forge_identity so its Path.home() points at the fake home."""
    import importlib
    import forge_employees
    import forge_identity
    importlib.reload(forge_employees)
    importlib.reload(forge_identity)
    return forge_identity


def test_get_identity_reads_name_and_emoji(identity, fake_home):
    _make_employee(fake_home, "designer", "Dora", "🎨")
    out = identity.get_identity("designer")
    assert out == {"id": "designer", "name": "Dora", "emoji": "🎨"}


def test_get_identity_falls_back_to_id(identity, fake_home):
    """No IDENTITY.md → name defaults to agent id, emoji empty."""
    # workspace exists but only with SOUL.md; no IDENTITY.md at all
    ws = fake_home / ".openclaw" / "workspace-foo"
    ws.mkdir(parents=True)
    (ws / "SOUL.md").write_text("x", encoding="utf-8")
    out = identity.get_identity("foo")
    assert out["name"] == "foo"
    assert out["emoji"] == ""


def test_get_identity_placeholder_treated_as_missing(identity, fake_home):
    """Template placeholders like '_(pick something you like)_' must not
    bleed into the UI as if they were real names."""
    _make_employee(
        fake_home, "sentry",
        name="_(pick something you like)_",
        emoji="_(your signature — pick one)_",
    )
    out = identity.get_identity("sentry")
    assert out["name"] == "sentry"   # fell back to id
    assert out["emoji"] == ""


def test_name_to_id_resolves_display_name(identity, fake_home):
    _make_employee(fake_home, "designer", "Dora", "🎨")
    assert identity.name_to_id("Dora") == "designer"
    assert identity.name_to_id("dora") == "designer"
    assert identity.name_to_id("designer") == "designer"


def test_name_to_id_resolves_compound_aliases(identity, fake_home):
    _make_employee(fake_home, "xiaoba", "小巴 (Xiaoba / Buffett)", "📈")
    assert identity.name_to_id("小巴") == "xiaoba"
    assert identity.name_to_id("Xiaoba") == "xiaoba"
    assert identity.name_to_id("Buffett") == "xiaoba"
    assert identity.name_to_id("buffett") == "xiaoba"  # case-insensitive


def test_name_to_id_returns_none_for_unknown(identity, fake_home):
    _make_employee(fake_home, "designer", "Dora", "🎨")
    assert identity.name_to_id("nobody") is None
    assert identity.name_to_id("") is None
    assert identity.name_to_id(None) is None


def test_list_identities_returns_all_employees(identity, fake_home):
    _make_employee(fake_home, "designer", "Dora", "🎨")
    _make_employee(fake_home, "judy", "Judy", "🔍")
    out = identity.list_identities()
    ids = [r["id"] for r in out]
    assert "designer" in ids
    assert "judy" in ids
    by_id = {r["id"]: r for r in out}
    assert by_id["designer"]["name"] == "Dora"
    assert by_id["judy"]["name"] == "Judy"
