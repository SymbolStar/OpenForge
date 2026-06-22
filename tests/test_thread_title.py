"""Tests for v0.10 thread title support.

Covers:
  - store.create_thread accepts title + optional content
  - empty content creates an empty thread (no posts) — PRD A-5
  - title is exposed via project_thread / summarize_thread / list_threads
  - legacy positional form still works (back-compat)
  - title length validation (1..80)
  - server POST /api/squads/<id>/threads accepts {title, content}
  - server allows empty content when title is provided
"""
# ruff: noqa: F811
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# share the `server`, `fake_home`, `store` fixtures
from tests.conftest import fake_home, server, store  # noqa: F401


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-OpenForge-UI": "1"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as r:
        return json.loads(r.read().decode("utf-8"))


# ─── store level ───────────────────────────────────────────────────


def test_store_create_thread_with_title_and_empty_content(store):
    store.create_squad({
        "id": "sq", "name": "sq", "members": ["scott"], "chair": "scott",
    })
    t = store.create_thread("sq", "scott", title="my new thread")
    assert t["title"] == "my new thread"
    assert t["post_count"] == 0
    assert t["posts"] == []
    # summarize / list expose title
    s = store.summarize_thread(t["thread_id"])
    assert s["title"] == "my new thread"
    lst = store.list_threads_for_squad("sq")
    assert lst[0]["title"] == "my new thread"


def test_store_create_thread_with_title_and_content(store):
    store.create_squad({
        "id": "sq2", "name": "sq2", "members": ["scott"], "chair": "scott",
    })
    t = store.create_thread(
        "sq2", "scott",
        title="hello",
        opening_content="first body",
    )
    assert t["title"] == "hello"
    assert t["post_count"] == 1
    assert t["posts"][0]["content"] == "first body"


def test_store_create_thread_title_length_validation(store):
    store.create_squad({
        "id": "sq3", "name": "sq3", "members": ["scott"], "chair": "scott",
    })
    # too long
    with pytest.raises(ValueError):
        store.create_thread("sq3", "scott", title="x" * 81)


def test_store_create_thread_legacy_positional_still_works(store):
    """Back-compat: old callers passing the opening post as 3rd positional arg."""
    store.create_squad({
        "id": "sq4", "name": "sq4", "members": ["scott"], "chair": "scott",
    })
    t = store.create_thread("sq4", "scott", "legacy opening line")
    # legacy form derives title from first line, post still created
    assert t["post_count"] == 1
    assert t["posts"][0]["content"] == "legacy opening line"
    assert t["title"] == "legacy opening line"


# ─── server level ──────────────────────────────────────────────────


def test_server_create_thread_with_title_only(server):
    _post(f"{server}/api/squads", {
        "id": "title_sq",
        "name": "title_sq",
        "members": ["scott", "milk"],
        "chair": "scott",
    })
    th = _post(f"{server}/api/squads/title_sq/threads", {
        "title": "design discussion",
        "created_by": "scott",
    })
    assert th["title"] == "design discussion"
    assert th["post_count"] == 0

    # list reflects title
    detail = _get(f"{server}/api/squads/title_sq")
    threads = detail["threads"]
    assert len(threads) == 1
    assert threads[0]["title"] == "design discussion"


def test_server_create_thread_with_title_and_content(server):
    _post(f"{server}/api/squads", {
        "id": "tc_sq",
        "name": "tc_sq",
        "members": ["scott", "milk"],
        "chair": "scott",
    })
    th = _post(f"{server}/api/squads/tc_sq/threads", {
        "title": "tc title",
        "content": "first body content",
        "created_by": "scott",
    })
    assert th["title"] == "tc title"
    assert th["post_count"] == 1
    assert th["posts"][0]["content"] == "first body content"


def test_server_create_thread_rejects_missing_title_and_content(server):
    _post(f"{server}/api/squads", {
        "id": "neg_sq",
        "name": "neg_sq",
        "members": ["scott"],
        "chair": "scott",
    })
    req = urllib.request.Request(
        f"{server}/api/squads/neg_sq/threads",
        data=json.dumps({"created_by": "scott"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-OpenForge-UI": "1"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=2)
    assert excinfo.value.code == 400


def test_server_create_thread_legacy_content_only(server):
    """Existing callers (no title field) still work."""
    _post(f"{server}/api/squads", {
        "id": "legacy_sq",
        "name": "legacy_sq",
        "members": ["scott"],
        "chair": "scott",
    })
    th = _post(f"{server}/api/squads/legacy_sq/threads", {
        "content": "legacy-style opening",
        "created_by": "scott",
    })
    assert th["post_count"] == 1
    # title is auto-derived from first line
    assert th["title"]
    assert th["title"] in "legacy-style opening"


# ─── rename (PATCH) ────────────────────────────────────────────────


def _patch(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-OpenForge-UI": "1"},
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read().decode("utf-8"))


def test_store_set_thread_title_renames(store):
    store.create_squad({
        "id": "rn1", "name": "rn1", "members": ["scott"], "chair": "scott",
    })
    t = store.create_thread("rn1", "scott", title="old name")
    store.set_thread_title(t["thread_id"], "new name")
    proj = store.project_thread(t["thread_id"])
    assert proj["title"] == "new name"
    # summarize / list also reflect new title
    assert store.summarize_thread(t["thread_id"])["title"] == "new name"
    assert store.list_threads_for_squad("rn1")[0]["title"] == "new name"


def test_store_set_thread_title_length_validation(store):
    store.create_squad({
        "id": "rn2", "name": "rn2", "members": ["scott"], "chair": "scott",
    })
    t = store.create_thread("rn2", "scott", title="ok")
    with pytest.raises(ValueError):
        store.set_thread_title(t["thread_id"], "x" * 81)


def test_server_patch_thread_title(server):
    _post(f"{server}/api/squads", {
        "id": "rnsv", "name": "rnsv", "members": ["scott"], "chair": "scott",
    })
    th = _post(f"{server}/api/squads/rnsv/threads", {
        "title": "before", "created_by": "scott",
    })
    tid = th["thread_id"]
    out = _patch(f"{server}/api/threads/{tid}", {"title": "after"})
    assert out["title"] == "after"
    # GET returns the new title
    assert _get(f"{server}/api/threads/{tid}")["title"] == "after"


def test_server_patch_thread_title_unknown_thread(server):
    req = urllib.request.Request(
        f"{server}/api/threads/th_does_not_exist",
        data=json.dumps({"title": "x"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-OpenForge-UI": "1"},
        method="PATCH",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=2)
    assert excinfo.value.code == 404


def test_server_patch_thread_title_missing_field(server):
    _post(f"{server}/api/squads", {
        "id": "rnmf", "name": "rnmf", "members": ["scott"], "chair": "scott",
    })
    th = _post(f"{server}/api/squads/rnmf/threads", {
        "title": "x", "created_by": "scott",
    })
    req = urllib.request.Request(
        f"{server}/api/threads/{th['thread_id']}",
        data=json.dumps({}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-OpenForge-UI": "1"},
        method="PATCH",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=2)
    assert excinfo.value.code == 400
