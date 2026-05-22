"""Tests for post_router: dispatch dedupe, implicit-mention-via-reply,
placeholder+supersede flow, error path, empty reply path, heal_polluted_mains."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture
def router(store, fake_home, monkeypatch):
    import agent_runtime
    monkeypatch.setattr(agent_runtime, "AGENTS_ROOT", fake_home / ".openclaw" / "agents")
    import post_router as pr
    # ensure no leftover in-flight state from previous tests
    with pr._inflight_lock:
        pr._inflight.clear()
    return pr


def _wait_for(predicate, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _make_thread(store, content="hello @milk please help"):
    store.ensure_default_squads()
    store.create_squad({"id": "sq", "name": "sq",
                        "members": ["milk", "scott"], "chair": "scott"})
    t = store.create_thread("sq", "scott", content)
    return t


# ─── enqueue_if_needed quick guards ──────────────────────────────────
def test_enqueue_ignores_non_scott(router, store):
    t = _make_thread(store, "hi")
    post = {"speaker": "milk", "post_id": "p1", "mentions": ["other"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is False


def test_enqueue_ignores_empty_post(router, store):
    assert router.enqueue_if_needed("th_x_x", {}) is False
    assert router.enqueue_if_needed("th_x_x", None) is False


def test_enqueue_no_mentions_no_parent(router, store):
    t = _make_thread(store, "no mention")
    post = {"speaker": "scott", "post_id": "p1", "mentions": []}
    assert router.enqueue_if_needed(t["thread_id"], post) is False


def test_enqueue_dedupes_mentions(router, store, monkeypatch):
    """Two @milk in one post → one dispatch."""
    calls: list[tuple] = []

    def fake_dispatch(tid, ag, trig):
        calls.append((tid, ag, trig))
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    t = _make_thread(store, "@milk hey @milk again")
    post = {"speaker": "scott", "post_id": "p1", "mentions": ["milk", "milk"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    assert len(calls) == 1


def test_implicit_mention_from_reply(router, store, monkeypatch):
    """scott replies to an agent post without @ → implicit mention."""
    calls: list[str] = []
    monkeypatch.setattr(router, "_dispatch",
                        lambda tid, ag, trig: calls.append(ag) or True)
    t = _make_thread(store, "@milk hi")
    # simulate prior milk reply
    milk_post = store.add_thread_post(t["thread_id"], "milk", "hi back")
    # scott replies to milk WITHOUT @
    reply = store.add_thread_post(t["thread_id"], "scott", "thanks",
                                  parent_post_id=milk_post["post_id"])
    post = {"speaker": "scott", "post_id": reply["post_id"],
            "mentions": [], "parent_post_id": milk_post["post_id"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    assert calls == ["milk"]


def test_implicit_mention_ignores_reserved(router, store, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(router, "_dispatch",
                        lambda tid, ag, trig: calls.append(ag) or True)
    t = _make_thread(store, "@milk hi")
    router_post = store.add_thread_post(t["thread_id"], "__router__", "⚠️ oops")
    post = {"speaker": "scott", "post_id": "x", "mentions": [],
            "parent_post_id": router_post["post_id"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is False
    assert calls == []


def test_implicit_mention_unknown_thread(router):
    assert router._implicit_mention_from_parent("th_dead_beef", "p_x") is None
    assert router._implicit_mention_from_parent("th_dead_beef", None) is None


# ─── _dispatch dedupe of in-flight pair ─────────────────────────────
def test_dispatch_dedupes_inflight(router, monkeypatch):
    """Two dispatches for same (tid, agent) → second drops."""
    started = threading.Event()
    release = threading.Event()

    def slow_route(tid, ag, trig):
        started.set()
        release.wait(timeout=3)
    monkeypatch.setattr(router, "_route_to_agent_safely", slow_route)
    a = router._dispatch("th_a_b", "milk", "p1")
    started.wait(timeout=2)
    b = router._dispatch("th_a_b", "milk", "p2")
    release.set()
    assert a is True
    assert b is False
    # let thread finish
    time.sleep(0.2)


# ─── full _route_to_agent flow ──────────────────────────────────────
def test_route_to_agent_success(router, store, monkeypatch):
    """call_agent returns text → final post appended, placeholder superseded."""
    monkeypatch.setattr(router, "call_agent", lambda ag, sid, prompt: "okay 👍")
    monkeypatch.setattr(router, "snapshot_main", lambda ag: None)
    monkeypatch.setattr(router, "restore_main", lambda ag, snap: True)

    t = _make_thread(store, "@milk help")
    scott_pid = t["posts"][0]["id"]
    trigger = {"post_id": scott_pid, "speaker": "scott", "content": "@milk help",
               "mentions": ["milk"]}
    router._route_to_agent(t["thread_id"], "milk", trigger)

    proj = store.project_thread(t["thread_id"])
    speakers = [p["speaker"] for p in proj["posts"] if not p.get("superseded")]
    assert "milk" in speakers
    # placeholder should be superseded
    placeholder = next(p for p in proj["posts"]
                       if p["speaker"] == "__router__" and "正在思考" in p["content"])
    assert placeholder["superseded"]


def test_route_to_agent_error(router, store, monkeypatch):
    def boom(ag, sid, prompt):
        from agent_runtime import AgentError
        raise AgentError("kaboom")
    monkeypatch.setattr(router, "call_agent", boom)
    monkeypatch.setattr(router, "snapshot_main", lambda ag: None)

    t = _make_thread(store, "@milk help")
    trigger = {"post_id": t["posts"][0]["id"], "speaker": "scott",
               "content": "@milk help", "mentions": ["milk"]}
    router._route_to_agent(t["thread_id"], "milk", trigger)
    proj = store.project_thread(t["thread_id"])
    assert any("没回复" in p["content"] for p in proj["posts"]
               if p["speaker"] == "__router__")


def test_route_to_agent_empty_reply(router, store, monkeypatch):
    monkeypatch.setattr(router, "call_agent", lambda ag, sid, prompt: "completed")
    monkeypatch.setattr(router, "snapshot_main", lambda ag: None)
    t = _make_thread(store, "@milk help")
    trigger = {"post_id": t["posts"][0]["id"], "speaker": "scott",
               "content": "@milk help", "mentions": ["milk"]}
    router._route_to_agent(t["thread_id"], "milk", trigger)
    proj = store.project_thread(t["thread_id"])
    assert any("空回复" in p["content"] for p in proj["posts"]
               if p["speaker"] == "__router__")


def test_route_to_agent_safely_skips_closed(router, store, monkeypatch):
    monkeypatch.setattr(router, "call_agent",
                        lambda *a, **k: pytest.fail("should not be called"))
    t = _make_thread(store, "@milk help")
    store.close_thread(t["thread_id"], closed_by="scott")
    router._route_to_agent_safely(t["thread_id"], "milk",
                                  t["posts"][0]["id"])


def test_route_to_agent_safely_unknown_thread(router, monkeypatch):
    monkeypatch.setattr(router, "call_agent",
                        lambda *a, **k: pytest.fail("should not be called"))
    router._route_to_agent_safely("th_dead_beef", "milk", "p_x")


def test_route_to_agent_safely_trigger_not_found(router, store, monkeypatch):
    monkeypatch.setattr(router, "call_agent",
                        lambda *a, **k: pytest.fail("should not be called"))
    t = _make_thread(store, "no mention")
    # nonexistent trigger pid and no scott post with mentions → return
    router._route_to_agent_safely(t["thread_id"], "milk", "p_ghost")


def test_find_trigger_fallback_picks_latest_scott_with_mentions(router, store):
    t = _make_thread(store, "@milk first")
    store.add_thread_post(t["thread_id"], "scott", "@milk second")
    proj = store.project_thread(t["thread_id"])
    found = router._find_trigger_post(proj, "")
    assert found and found["content"] == "@milk second"


def test_build_prompt_contains_agent_and_content(router, store):
    t = _make_thread(store, "@milk please fix CI")
    trigger = {"post_id": t["posts"][0]["id"], "speaker": "scott",
               "content": "@milk please fix CI"}
    p = router._build_prompt(t["thread_id"], "milk", trigger)
    assert "milk" in p
    assert "please fix CI" in p
    assert "completed" in p


def test_build_prompt_injects_status_bundle(router, store, fake_home):
    import forge_context
    forge_context.write_status("milk", "# Milk\n\n## 当前焦点\nCI 已修复 ✍ 16:35\n")
    t = _make_thread(store, "@milk fix CI")
    trigger = {"post_id": t["posts"][0]["id"], "speaker": "scott",
               "content": "@milk fix CI"}
    p = router._build_prompt(t["thread_id"], "milk", trigger)
    # Bundle preamble appears before the standard prompt.
    assert "OpenForge 已预查" in p
    assert "CI 已修复" in p
    # And STATUS update hint is in the prompt
    assert "/api/agents/milk/status" in p


def test_build_prompt_no_status_no_bundle(router, store):
    t = _make_thread(store, "@milk hi")
    trigger = {"post_id": t["posts"][0]["id"], "speaker": "scott", "content": "@milk hi"}
    p = router._build_prompt(t["thread_id"], "milk", trigger)
    # No bundle preamble when no sources available
    assert "OpenForge 已预查" not in p
    # Standard prompt still present
    assert "completed" in p


# ─── heal_polluted_mains ─────────────────────────────────────────────
def test_heal_polluted_mains(router, fake_home):
    sess = fake_home / ".openclaw" / "agents" / "milk" / "sessions"
    sess.mkdir(parents=True)
    sj = sess / "sessions.json"
    sj.write_text(json.dumps({
        "agent:milk:main": {"sessionId": "forge-th_xx-milk",
                            "sessionFile": "/tmp/x"},
    }))
    (sess / "clean-z.jsonl").write_text("")
    healed = router.heal_polluted_mains(["milk", "ghost"])
    assert healed == ["milk"]
    d = json.loads(sj.read_text())
    assert d["agent:milk:main"]["sessionId"] == "clean-z"
    assert d["agent:milk:main"]["healedByOpenForge"] is True


def test_heal_skips_already_clean(router, fake_home):
    sess = fake_home / ".openclaw" / "agents" / "milk" / "sessions"
    sess.mkdir(parents=True)
    sj = sess / "sessions.json"
    sj.write_text(json.dumps({
        "agent:milk:main": {"sessionId": "clean-x", "sessionFile": "/tmp/x"},
    }))
    assert router.heal_polluted_mains(["milk"]) == []


def test_heal_skips_corrupt_sessions_json(router, fake_home):
    sess = fake_home / ".openclaw" / "agents" / "milk" / "sessions"
    sess.mkdir(parents=True)
    (sess / "sessions.json").write_text("not json")
    assert router.heal_polluted_mains(["milk"]) == []


def test_record_crash_swallows_store_failure(router, store, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("nope")
    monkeypatch.setattr(store, "add_thread_post", boom)
    router._record_crash("th_x_y", "milk", RuntimeError("boom"))  # no raise


# ─── full enqueue → background thread → reply integration ────────
def test_enqueue_full_flow(router, store, monkeypatch):
    monkeypatch.setattr(router, "call_agent",
                        lambda ag, sid, prompt: f"[reply from {ag}]")
    monkeypatch.setattr(router, "snapshot_main", lambda ag: None)
    monkeypatch.setattr(router, "restore_main", lambda ag, snap: True)

    t = _make_thread(store, "@milk integration test")
    # the create_thread above already enqueued (scott + @milk),
    # but we don't go through HTTP; call enqueue manually
    post = t["posts"][0]
    post["post_id"] = post["id"]
    router.enqueue_if_needed(t["thread_id"], post)
    # wait for the worker to land its reply
    def _has_milk_reply():
        proj = store.project_thread(t["thread_id"])
        return any(p["speaker"] == "milk" for p in (proj["posts"] or []))
    assert _wait_for(_has_milk_reply), "milk reply never appeared"
    proj = store.project_thread(t["thread_id"])
    milk = next(p for p in proj["posts"] if p["speaker"] == "milk")
    assert "[reply from milk]" in milk["content"]
    assert milk["parent_post_id"] == post["post_id"]
