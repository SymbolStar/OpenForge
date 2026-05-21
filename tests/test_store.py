"""Smoke + behaviour tests for the JSONL event store."""
from __future__ import annotations

import pytest


@pytest.fixture
def milk_squad(store):
    store.ensure_default_squads()
    store.create_squad({
        "id": "milk-eng",
        "name": "milk-eng",
        "members": ["milk", "sentry", "bugfix"],
        "chair": "milk",
    })
    return "milk-eng"


def test_extract_mentions_basic(store):
    assert store.extract_mentions("hi @milk and @sentry") == ["milk", "sentry"]
    # dedup + order preserved
    assert store.extract_mentions("@milk @milk @sentry") == ["milk", "sentry"]


def test_thread_create_then_project(store, milk_squad):
    t = store.create_thread(milk_squad, "scott", "first post @milk")
    assert t["thread_id"].startswith("th_")
    assert t["post_count"] == 1
    assert t["posts"][0]["speaker"] == "scott"
    assert t["posts"][0]["mentions"] == ["milk"]


def test_add_post_with_parent(store, milk_squad):
    t = store.create_thread(milk_squad, "scott", "root post")
    root_pid = t["posts"][0]["id"]
    store.add_thread_post(t["thread_id"], "scott", "reply", parent_post_id=root_pid)
    refreshed = store.project_thread(t["thread_id"])
    assert refreshed["post_count"] == 2
    assert refreshed["posts"][1]["parent_post_id"] == root_pid


def test_supersede_marks_old_post(store, milk_squad):
    t = store.create_thread(milk_squad, "scott", "to be replaced")
    pid = t["posts"][0]["id"]
    new = store.add_thread_post(t["thread_id"], "scott", "replacement")
    store.supersede_thread_post(t["thread_id"], pid, by_post_id=new["post_id"])
    refreshed = store.project_thread(t["thread_id"])
    live = [p for p in refreshed["posts"] if not p["superseded"]]
    assert len(live) == 1
    assert live[0]["id"] == new["post_id"]


def test_reactions_toggle(store, milk_squad):
    t = store.create_thread(milk_squad, "scott", "react to me")
    pid = t["posts"][0]["id"]

    out = store.toggle_reaction(t["thread_id"], pid, "👍", actor="scott")
    assert out == {"👍": ["scott"]}

    out = store.toggle_reaction(t["thread_id"], pid, "👍", actor="scott")
    assert out == {}

    store.toggle_reaction(t["thread_id"], pid, "🚀", actor="judy")
    store.toggle_reaction(t["thread_id"], pid, "🚀", actor="scott")
    refreshed = store.project_thread(t["thread_id"])
    post = refreshed["posts_by_id"][pid]
    assert post["reactions"] == {"🚀": ["judy", "scott"]}


def test_reaction_rejects_bad_emoji(store, milk_squad):
    t = store.create_thread(milk_squad, "scott", "x")
    pid = t["posts"][0]["id"]
    with pytest.raises(ValueError):
        store.toggle_reaction(t["thread_id"], pid, "", actor="scott")
    with pytest.raises(ValueError):
        store.toggle_reaction(t["thread_id"], pid, "a b", actor="scott")  # whitespace


def test_squad_archive_filter(store):
    store.ensure_default_squads()
    store.create_squad({"id": "ephemeral", "members": ["milk"], "chair": "milk", "name": "x"})
    store.update_squad("ephemeral", {"archived": True})
    visible = store.list_squads(include_archived=False)
    assert all(s["id"] != "ephemeral" for s in visible)
    full = store.list_squads(include_archived=True)
    assert any(s["id"] == "ephemeral" for s in full)
