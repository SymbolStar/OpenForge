"""Store-level tests for v0.5 PR-A: squad.project_dir + thread.extra_projects.

Covers:
  * round-trip on a new squad with project_dir set
  * round-trip on a new squad without project_dir (defaults to None)
  * graceful migration of a legacy squads.json that lacks the field
  * update_squad accepts None / empty string (clears) and rejects non-strings
  * thread.extra_projects defaults to [] in the projection
"""
# ruff: noqa: F811
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_create_squad_with_project_dir(store):
    store.ensure_default_squads()
    sq = store.create_squad({
        "id": "openforge-dev",
        "name": "x",
        "members": ["scott"],
        "chair": "scott",
        "project_dir": "/Volumes/DevDisk/symbol/openforge",
    })
    assert sq["project_dir"] == "/Volumes/DevDisk/symbol/openforge"

    again = store.get_squad("openforge-dev")
    assert again["project_dir"] == "/Volumes/DevDisk/symbol/openforge"


def test_create_squad_without_project_dir_defaults_none(store):
    store.ensure_default_squads()
    sq = store.create_squad({
        "id": "discuss-only",
        "name": "x",
        "members": ["scott"],
        "chair": "scott",
    })
    assert sq["project_dir"] is None
    assert store.get_squad("discuss-only")["project_dir"] is None


def test_update_squad_sets_and_clears_project_dir(store):
    store.ensure_default_squads()
    store.create_squad({
        "id": "team-x",
        "members": ["scott"],
        "chair": "scott",
        "name": "x",
    })
    # set
    out = store.update_squad("team-x", {"project_dir": "/abs/path"})
    assert out["project_dir"] == "/abs/path"
    # clear via None
    out = store.update_squad("team-x", {"project_dir": None})
    assert out["project_dir"] is None
    # clear via empty string
    store.update_squad("team-x", {"project_dir": "/abs/path"})
    out = store.update_squad("team-x", {"project_dir": ""})
    assert out["project_dir"] is None
    # invalid type → ValueError
    with pytest.raises(ValueError):
        store.update_squad("team-x", {"project_dir": 123})


def test_legacy_squads_json_missing_field_is_migrated_on_read(store):
    """A squads.json written before PR-A (no project_dir key) must still load
    and surface project_dir == None on every squad."""
    # Manufacture a pre-PR-A squads.json directly on disk.
    legacy = {
        "version": 1,
        "squads": {
            "ancient": {
                "id": "ancient",
                "chair": "scott",
                "members": ["scott"],
                "emoji": "#",
                "name": "ancient",
                "description": "",
            }
        },
    }
    store.ensure_default_squads()  # ensures FORGE_DIR exists
    Path(store.SQUADS_PATH).write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    sq = store.get_squad("ancient")
    assert sq is not None
    assert sq["project_dir"] is None
    # list_squads should also surface the field
    listed = store.list_squads()
    assert any(s["id"] == "ancient" and s["project_dir"] is None for s in listed)


def test_thread_projection_includes_extra_projects(store):
    store.ensure_default_squads()
    store.create_squad({
        "id": "alpha",
        "members": ["scott"],
        "chair": "scott",
        "name": "alpha",
    })
    t = store.create_thread("alpha", "scott", "hello")
    proj = store.project_thread(t["thread_id"])
    assert "extra_projects" in proj
    assert proj["extra_projects"] == []
