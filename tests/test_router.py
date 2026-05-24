"""Tests for post_router decision logic (no real subprocess spawning)."""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def squad(store):
    store.ensure_default_squads()
    store.create_squad({
        "id": "milk-eng",
        "name": "milk-eng",
        "members": ["milk", "sentry"],
        "chair": "milk",
    })
    return "milk-eng"


@pytest.fixture
def thread_with_milk_post(store, squad):
    """A thread that already has one milk post; returns (tid, milk_post_id)."""
    t = store.create_thread(squad, "scott", "kick off @milk")
    # simulate milk's reply landing
    out = store.add_thread_post(t["thread_id"], "milk", "hi from milk")
    return t["thread_id"], out["post_id"]


def test_enqueue_routes_for_non_scott_employee(router, store, squad):
    """V1.1: any non-router employee can trigger routing."""
    t = store.create_thread(squad, "scott", "x")
    post = {"speaker": "milk", "mentions": ["sentry"], "post_id": "p_x", "parent_post_id": None}
    assert router.enqueue_if_needed(t["thread_id"], post) is True


def test_enqueue_skips_no_mentions_no_parent(router, store, squad):
    t = store.create_thread(squad, "scott", "x")
    post = {"speaker": "scott", "mentions": [], "post_id": "p_x", "parent_post_id": None}
    assert router.enqueue_if_needed(t["thread_id"], post) is False


def test_implicit_mention_from_reply_to_agent(router, store, thread_with_milk_post, monkeypatch):
    tid, milk_pid = thread_with_milk_post
    # patch _dispatch so we observe routing decisions without spawning threads
    seen = []
    monkeypatch.setattr(router, "_dispatch", lambda t, a, p: seen.append((t, a, p)) or True)
    post = {
        "speaker": "scott", "mentions": [],
        "post_id": "p_reply", "parent_post_id": milk_pid,
    }
    assert router.enqueue_if_needed(tid, post) is True
    assert seen == [(tid, "milk", "p_reply")]


def test_implicit_mention_ignores_reply_to_scott(router, store, squad, monkeypatch):
    t = store.create_thread(squad, "scott", "root")
    root_pid = t["posts"][0]["id"]
    seen = []
    monkeypatch.setattr(router, "_dispatch", lambda *a: seen.append(a) or True)
    post = {
        "speaker": "scott", "mentions": [],
        "post_id": "p_self", "parent_post_id": root_pid,
    }
    assert router.enqueue_if_needed(t["thread_id"], post) is False
    assert seen == []


def test_implicit_mention_ignores_reply_to_router(router, store, squad, monkeypatch):
    t = store.create_thread(squad, "scott", "root")
    # router placeholder post
    ph = store.add_thread_post(t["thread_id"], "__router__", "⏳ thinking…")
    seen = []
    monkeypatch.setattr(router, "_dispatch", lambda *a: seen.append(a) or True)
    post = {
        "speaker": "scott", "mentions": [],
        "post_id": "p_x", "parent_post_id": ph["post_id"],
    }
    assert router.enqueue_if_needed(t["thread_id"], post) is False


def test_explicit_mention_overrides_implicit(router, store, thread_with_milk_post, monkeypatch):
    tid, milk_pid = thread_with_milk_post
    seen = []
    monkeypatch.setattr(router, "_dispatch", lambda t, a, p: seen.append(a) or True)
    # reply to milk's post but @sentry — sentry wins, not milk
    post = {
        "speaker": "scott", "mentions": ["sentry"],
        "post_id": "p_x", "parent_post_id": milk_pid,
    }
    assert router.enqueue_if_needed(tid, post) is True
    assert seen == ["sentry"]


def test_explicit_mention_fans_out_uniquely(router, store, squad, monkeypatch):
    t = store.create_thread(squad, "scott", "x")
    seen = []
    monkeypatch.setattr(router, "_dispatch", lambda tt, a, p: seen.append(a) or True)
    post = {
        "speaker": "scott",
        "mentions": ["milk", "sentry", "milk"],  # dup
        "post_id": "p_x",
        "parent_post_id": None,
    }
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    assert seen == ["milk", "sentry"]


def test_dispatch_dedupe_for_inflight_pair(router, store, squad):
    """Second dispatch for same (tid, agent) while first still running returns False."""
    t = store.create_thread(squad, "scott", "x")
    # Manually populate _inflight to simulate an in-progress worker
    router._inflight.add((t["thread_id"], "milk"))
    try:
        assert router._dispatch(t["thread_id"], "milk", "p_x") is False
    finally:
        router._inflight.discard((t["thread_id"], "milk"))


def test_end_to_end_with_fake_openclaw(router, store, squad):
    """Real fan-out path with the fake_openclaw.sh mock. Waits for reply."""
    t = store.create_thread(squad, "scott", "ping @milk")
    # enqueue manually with the opening post
    opening = t["posts"][0]
    opening["post_id"] = opening["id"]
    assert router.enqueue_if_needed(t["thread_id"], opening) is True
    # wait up to 5s for milk to reply
    deadline = time.time() + 5
    while time.time() < deadline:
        refreshed = store.project_thread(t["thread_id"])
        replies = [p for p in refreshed["posts"] if p["speaker"] == "milk" and not p["superseded"]]
        if replies:
            assert "[mock milk]" in replies[0]["content"]
            return
        time.sleep(0.2)
    raise AssertionError("milk reply never landed")
