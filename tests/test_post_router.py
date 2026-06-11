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
    # Scott 2026-06-09: enqueue_if_needed now drops @mentions that are
    # not known employees. Tests use synthetic agent ids (milk, sherry,
    # other, …) under a fake $HOME with no real agent runtimes, so
    # forge_employees.is_employee returns False for everything. Stub it
    # to True here so the routing tests continue to exercise the actual
    # dispatch path; test_post_router.test_unknown_mention_is_skipped
    # restores the real lookup for the dedicated unknown-id case.
    import forge_employees
    monkeypatch.setattr(pr.forge_employees, "is_employee", lambda _id: True)
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
def test_enqueue_routes_for_any_employee_speaker(router, store, monkeypatch):
    """V1.1: any non-router speaker can trigger routing."""
    calls: list[str] = []
    def fake_dispatch(tid, ag, trig):
        calls.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    t = _make_thread(store, "@other ping")
    post = {"speaker": "milk", "post_id": "p1", "mentions": ["other"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    assert calls == ["other"]


def test_enqueue_ignores_router_speaker(router, store):
    """__router__ posts must never re-trigger routing (loop guard)."""
    t = _make_thread(store, "hi")
    post = {"speaker": "__router__", "post_id": "p1", "mentions": ["milk"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is False


def test_enqueue_drops_self_mention(router, store, monkeypatch):
    """Agent @ing themselves is a no-op."""
    calls: list[str] = []
    def fake_dispatch(tid, ag, trig):
        calls.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    t = _make_thread(store, "@milk note to self")
    post = {"speaker": "milk", "post_id": "p1", "mentions": ["milk", "sherry"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    assert calls == ["sherry"]


def test_enqueue_drops_scott_mention_from_agent(router, store, monkeypatch):
    """@scott is not a routable endpoint."""
    calls: list[str] = []
    def fake_dispatch(tid, ag, trig):
        calls.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    t = _make_thread(store, "@scott @sherry fyi")
    post = {"speaker": "judy", "post_id": "p1", "mentions": ["scott", "sherry"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    assert calls == ["sherry"]


def test_enqueue_ignores_empty_speaker(router, store):
    """Empty speaker -> ignore."""
    t = _make_thread(store, "hi")
    post = {"speaker": "", "post_id": "p1", "mentions": ["milk"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is False


def test_enqueue_resolves_display_name_to_agent_id(router, store, monkeypatch, fake_home):
    """V1.2: @<DisplayName> resolves to the canonical agent id before dispatch.
    Users can write @Dora and the router wakes up `designer`."""
    # Set up a real employee whose display name doesn't match its id.
    ws = fake_home / ".openclaw" / "workspace-designer"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "SOUL.md").write_text("x", encoding="utf-8")
    (ws / "IDENTITY.md").write_text("- **Name:** Dora\n", encoding="utf-8")
    (fake_home / ".openclaw" / "agents" / "designer").mkdir(parents=True, exist_ok=True)
    # Reload identity module so it sees the new workspace.
    import importlib

    import forge_employees
    import forge_identity
    importlib.reload(forge_employees)
    importlib.reload(forge_identity)
    # Re-bind the module reference on the router we already loaded.
    router.forge_identity = forge_identity

    calls: list[str] = []

    def fake_dispatch(tid, ag, trig):
        calls.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    t = _make_thread(store, "hi @Dora")
    post = {"speaker": "scott", "post_id": "p1", "mentions": ["Dora"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    assert calls == ["designer"]


def test_enqueue_default_chair_when_scott_omits_mention(router, store, monkeypatch):
    """V1.1 (Scott 2026-05-24): scott posts with no @ → default route
    to the thread's squad chair."""
    calls: list[str] = []

    def fake_dispatch(tid, ag, trig):
        calls.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    store.ensure_default_squads()
    store.create_squad({"id": "sqd", "name": "sqd",
                        "members": ["sherry", "scott"], "chair": "sherry"})
    t = store.create_thread("sqd", "scott", "naked message no @")
    post = {"speaker": "scott", "post_id": "p1", "mentions": []}
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    assert calls == ["sherry"]


def test_enqueue_default_chair_does_not_fire_for_agent(router, store, monkeypatch):
    """Default-to-chair fallback is scott-only; agent posts without @ stay quiet."""
    calls: list[str] = []

    def fake_dispatch(tid, ag, trig):
        calls.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    store.ensure_default_squads()
    store.create_squad({"id": "sqd2", "name": "sqd2",
                        "members": ["sherry", "judy", "scott"], "chair": "sherry"})
    t = store.create_thread("sqd2", "scott", "hi")
    post = {"speaker": "judy", "post_id": "p1", "mentions": []}
    assert router.enqueue_if_needed(t["thread_id"], post) is False
    assert calls == []


def test_enqueue_default_chair_yields_to_explicit_reply(router, store, monkeypatch):
    """Scott reply to agent post → still routes to that agent, not chair."""
    calls: list[str] = []

    def fake_dispatch(tid, ag, trig):
        calls.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    store.ensure_default_squads()
    store.create_squad({"id": "sqd3", "name": "sqd3",
                        "members": ["sherry", "judy", "scott"], "chair": "sherry"})
    t = store.create_thread("sqd3", "scott", "@judy hi")
    judy_post = store.add_thread_post(t["thread_id"], "judy", "hello")
    post = {"speaker": "scott", "post_id": "p1", "mentions": [],
            "parent_post_id": judy_post["post_id"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    assert calls == ["judy"]  # parent wins over chair


def test_enqueue_ignores_empty_post(router, store):
    assert router.enqueue_if_needed("th_x_x", {}) is False
    assert router.enqueue_if_needed("th_x_x", None) is False


def test_enqueue_no_mentions_no_parent(router, store):
    t = _make_thread(store, "no mention")
    post = {"speaker": "scott", "post_id": "p1", "mentions": []}
    assert router.enqueue_if_needed(t["thread_id"], post) is False


def test_chair_token_resolves_to_squad_chair(router, store, monkeypatch):
    """@chair → dispatched to squad's actual chair (PRD-v1.0 §2)."""
    calls: list[str] = []

    def fake_dispatch(tid, ag, trig):
        calls.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    store.ensure_default_squads()
    store.create_squad({"id": "sqc", "name": "sqc",
                        "members": ["sherry", "scott"], "chair": "sherry"})
    t = store.create_thread("sqc", "scott", "@chair fyi")
    post = {"speaker": "scott", "post_id": "p1", "mentions": ["chair"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    assert calls == ["sherry"]


def test_chair_token_collapses_with_explicit_chair_name(router, store, monkeypatch):
    """@chair + @sherry (chair=sherry) → single dispatch."""
    calls: list[str] = []

    def fake_dispatch(tid, ag, trig):
        calls.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    store.ensure_default_squads()
    store.create_squad({"id": "sqc2", "name": "sqc2",
                        "members": ["sherry", "scott"], "chair": "sherry"})
    t = store.create_thread("sqc2", "scott", "@chair @sherry hey")
    post = {"speaker": "scott", "post_id": "p1",
            "mentions": ["chair", "sherry"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    assert calls == ["sherry"]


def test_chair_token_dropped_when_chair_recursive(router, store, monkeypatch):
    """Squad whose chair is literally 'chair' → token is dropped, not
    routed (avoids the 'Unknown agent id chair' error post)."""
    calls: list[str] = []

    def fake_dispatch(tid, ag, trig):
        calls.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    store.ensure_default_squads()
    store.create_squad({"id": "sqc3", "name": "sqc3",
                        "members": ["chair", "scott"], "chair": "chair"})
    t = store.create_thread("sqc3", "scott", "@chair help")
    post = {"speaker": "scott", "post_id": "p1", "mentions": ["chair"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is False
    assert calls == []


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


def test_enqueue_drops_unknown_mention(router, store, monkeypatch):
    """Scott 2026-06-09: @name not in employees list → no dispatch, no chip.

    Previously such mentions would still spawn a worker which produced a
    red "Unknown agent id" failure chip. We now silently drop them at
    the router. Only the real employee in the same post should route.
    """
    calls: list[str] = []
    def fake_dispatch(tid, ag, trig):
        calls.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    # Override the fixture's blanket is_employee stub: only 'milk' is real.
    monkeypatch.setattr(
        router.forge_employees, "is_employee",
        lambda aid: aid == "milk",
    )
    t = _make_thread(store, "@name not real, @milk is")
    post = {"speaker": "scott", "post_id": "p1",
            "mentions": ["name", "milk"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    assert calls == ["milk"]


def test_enqueue_all_unknown_mentions_returns_false(router, store, monkeypatch):
    """If every @mention is unknown, enqueue returns False (no dispatch)."""
    calls: list[str] = []
    def fake_dispatch(tid, ag, trig):
        calls.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", fake_dispatch)
    monkeypatch.setattr(
        router.forge_employees, "is_employee", lambda _id: False,
    )
    t = _make_thread(store, "@nobody @ghost")
    post = {"speaker": "scott", "post_id": "p1",
            "mentions": ["nobody", "ghost"]}
    assert router.enqueue_if_needed(t["thread_id"], post) is False
    assert calls == []


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
def test_agent_reply_with_mention_re_enqueues(router, store, monkeypatch):
    """V1.1: an agent's own reply containing @mentions must be re-fed
    through the router so downstream agents wake up. Without this,
    chair-style dispatch ('@designer @alice please look') was a dead ping."""
    monkeypatch.setattr(router, "call_agent",
                        lambda ag, sid, prompt, **_kw: "好，拉他们进来。 @sherry @milk")
    monkeypatch.setattr(router, "snapshot_main", lambda ag: None)
    monkeypatch.setattr(router, "restore_main", lambda ag, snap: True)

    dispatched: list[str] = []

    def spy_dispatch(tid, ag, trig):
        dispatched.append(ag)
        return True
    monkeypatch.setattr(router, "_dispatch", spy_dispatch)

    t = _make_thread(store, "@judy please dispatch")
    trigger = {"post_id": t["posts"][0]["id"], "speaker": "scott",
               "content": "@judy please dispatch", "mentions": ["judy"]}
    router._route_to_agent(t["thread_id"], "judy", trigger)

    # judy's reply mentioned sherry and milk; both should have been
    # re-routed via the agent-reply re-enqueue path.
    assert "sherry" in dispatched
    assert "milk" in dispatched


def test_route_to_agent_success(router, store, monkeypatch):
    """call_agent returns text -> final post appended, chip marked done."""
    monkeypatch.setattr(router, "call_agent", lambda ag, sid, prompt, **_kw: "okay 👍")
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
    chip = next(p for p in proj["posts"] if p.get("post_type") == "status_chip")
    assert chip["phase"] == "done"
    assert chip["duration_ms"] is not None


def test_route_to_agent_error(router, store, monkeypatch):
    def boom(ag, sid, prompt, **_kw):
        from agent_runtime import AgentError
        raise AgentError("kaboom")
    monkeypatch.setattr(router, "call_agent", boom)
    monkeypatch.setattr(router, "snapshot_main", lambda ag: None)

    t = _make_thread(store, "@milk help")
    trigger = {"post_id": t["posts"][0]["id"], "speaker": "scott",
               "content": "@milk help", "mentions": ["milk"]}
    router._route_to_agent(t["thread_id"], "milk", trigger)
    proj = store.project_thread(t["thread_id"])
    chips = [p for p in proj["posts"] if p.get("post_type") == "status_chip"]
    assert len(chips) == 1
    assert chips[0]["phase"] == "failed"
    assert "kaboom" in chips[0]["error"]
    assert not any(p["speaker"] == "__router__" and p.get("post_type") != "status_chip"
                   for p in proj["posts"])


def test_route_to_agent_empty_reply(router, store, monkeypatch):
    monkeypatch.setattr(router, "call_agent", lambda ag, sid, prompt, **_kw: "completed")
    monkeypatch.setattr(router, "snapshot_main", lambda ag: None)
    t = _make_thread(store, "@milk help")
    trigger = {"post_id": t["posts"][0]["id"], "speaker": "scott",
               "content": "@milk help", "mentions": ["milk"]}
    router._route_to_agent(t["thread_id"], "milk", trigger)
    proj = store.project_thread(t["thread_id"])
    chip = next(p for p in proj["posts"] if p.get("post_type") == "status_chip")
    assert chip["phase"] == "skipped"
    assert "reason" not in chip


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


def test_build_prompt_teaches_on_demand_memory_search(router, store):
    """v0.9.1: system prompt must teach agent to call memory_search on demand,
    since OpenForge no longer pre-fetches memory into the bundle."""
    t = _make_thread(store, "@milk hi")
    trigger = {"post_id": t["posts"][0]["id"], "speaker": "scott", "content": "@milk hi"}
    p = router._build_prompt(t["thread_id"], "milk", trigger)
    assert "memory_search" in p
    assert "ask-on-demand" in p


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
                        lambda ag, sid, prompt, **_kw: f"[reply from {ag}]")
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


# ─── PR-B1: [project] section conditional injection ─────────────────

def test_project_section_omitted_when_squad_has_no_project_dir(fake_home, store):  # noqa: F811
    import post_router
    sq = store.create_squad({"id": "noproj", "name": "noproj", "members": ["scott"], "chair": "scott"})
    t = store.create_thread(sq["id"], "scott", title="t", opening_content="hi")
    out = post_router._render_project_section(t["thread_id"])
    assert out == "", "no project_dir → no segment"


def test_project_section_ok_banner_when_path_valid(fake_home, store, tmp_path):  # noqa: F811
    import forge_project
    import post_router
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    forge_project.invalidate()  # ensure fresh
    sq = store.create_squad({
        "id": "ok", "name": "ok", "members": ["scott"], "chair": "scott",
        "project_dir": str(repo),
    })
    t = store.create_thread(sq["id"], "scott", title="t", opening_content="hi")
    out = post_router._render_project_section(t["thread_id"])
    assert out.startswith("[project]"), out
    assert "目标 repo 已由当前 squad 锁定" in out
    # Encapsulation: must NOT leak the path or the env var name.
    assert str(repo) not in out
    assert "OPENFORGE_PROJECT_DIR" not in out


def test_project_section_warning_when_path_invalid(fake_home, store, tmp_path):  # noqa: F811
    import forge_project
    import post_router
    bare = tmp_path / "no-git"
    bare.mkdir()  # exists but no .git
    forge_project.invalidate()
    sq = store.create_squad({
        "id": "bad", "name": "bad", "members": ["scott"], "chair": "scott",
        "project_dir": str(bare),
    })
    t = store.create_thread(sq["id"], "scott", title="t", opening_content="hi")
    out = post_router._render_project_section(t["thread_id"])
    assert out.startswith("[project] ⚠️"), out
    assert "worktree 规则本轮已禁用" in out
    # Path IS shown in the warning, so scott can spot the typo.
    assert str(bare) in out


# ─── PR-B2: spawn env (OPENFORGE_PROJECT_DIR) ──────────────────────

def test_spawn_env_none_when_no_project_dir(fake_home, store):  # noqa: F811
    import post_router
    sq = store.create_squad({"id": "noprj", "name": "noprj", "members": ["scott"], "chair": "scott"})
    t = store.create_thread(sq["id"], "scott", title="t", opening_content="hi")
    assert post_router._spawn_env_for_thread(t["thread_id"]) is None


def test_spawn_env_none_when_path_invalid(fake_home, store, tmp_path):  # noqa: F811
    import forge_project
    import post_router
    bare = tmp_path / "no-git"
    bare.mkdir()  # exists but no .git → invalid
    forge_project.invalidate()
    sq = store.create_squad({
        "id": "bad", "name": "bad", "members": ["scott"], "chair": "scott",
        "project_dir": str(bare),
    })
    t = store.create_thread(sq["id"], "scott", title="t", opening_content="hi")
    # Invariant: never inject env when path is invalid — script would silently
    # operate on the wrong dir if we did.
    assert post_router._spawn_env_for_thread(t["thread_id"]) is None


def test_spawn_env_injects_path_when_valid(fake_home, store, tmp_path):  # noqa: F811
    import forge_project
    import post_router
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    forge_project.invalidate()
    sq = store.create_squad({
        "id": "ok", "name": "ok", "members": ["scott"], "chair": "scott",
        "project_dir": str(repo),
    })
    t = store.create_thread(sq["id"], "scott", title="t", opening_content="hi")
    env = post_router._spawn_env_for_thread(t["thread_id"])
    assert env == {"OPENFORGE_PROJECT_DIR": str(repo)}


def test_call_agent_propagates_extra_env(fake_home, tmp_path, monkeypatch):  # noqa: F811
    """End-to-end: call_agent should pass extra_env into the subprocess env."""
    import agent_runtime as ar
    # Fake openclaw that echoes one env var as the JSON result.
    fake = tmp_path / "fake_openclaw.sh"
    fake.write_text(
        '#!/bin/sh\n'
        'printf \'{"payloads":[{"text":"got=%s"}]}\' "${OPENFORGE_PROJECT_DIR:-MISSING}"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setattr(ar, "OPENCLAW_BIN", str(fake))
    monkeypatch.setattr(ar, "AGENT_TIMEOUT", 5)
    out = ar.call_agent("milk", "sid", "hi", extra_env={"OPENFORGE_PROJECT_DIR": "/tmp/foo"})
    assert out == "got=/tmp/foo"


# ─── PR-C2: worktree rule preamble (only on valid project_dir) ─────

def test_project_section_includes_worktree_rule_when_valid(fake_home, store, tmp_path):  # noqa: F811
    import forge_project
    import post_router
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    forge_project.invalidate()
    sq = store.create_squad({
        "id": "ok", "name": "ok", "members": ["scott"], "chair": "scott",
        "project_dir": str(repo),
    })
    t = store.create_thread(sq["id"], "scott", title="t", opening_content="hi")
    out = post_router._render_project_section(t["thread_id"])
    # Rule is gated on valid project — appears alongside the OK banner.
    assert "[代码改动规则]" in out
    assert "openforge-worktree add" in out
    assert "openforge-worktree rm" in out
    # Encapsulation: no path, no env var name in agent-visible text.
    assert str(repo) not in out
    assert "OPENFORGE_PROJECT_DIR" not in out
    # Hard line cap: 12 lines max per alice's review standard.
    assert len(out.splitlines()) <= 12, f"preamble exceeded 12-line cap: {len(out.splitlines())} lines"


def test_project_section_rule_absent_when_invalid(fake_home, store, tmp_path):  # noqa: F811
    """Invariant: a broken project must NOT show a rule that would fail."""
    import forge_project
    import post_router
    bare = tmp_path / "no-git"
    bare.mkdir()
    forge_project.invalidate()
    sq = store.create_squad({
        "id": "bad", "name": "bad", "members": ["scott"], "chair": "scott",
        "project_dir": str(bare),
    })
    t = store.create_thread(sq["id"], "scott", title="t", opening_content="hi")
    out = post_router._render_project_section(t["thread_id"])
    assert "[代码改动规则]" not in out
    assert "openforge-worktree" not in out


def test_project_section_rule_absent_when_unset(fake_home, store):  # noqa: F811
    import post_router
    sq = store.create_squad({"id": "discuss", "name": "d", "members": ["scott"], "chair": "scott"})
    t = store.create_thread(sq["id"], "scott", title="t", opening_content="hi")
    out = post_router._render_project_section(t["thread_id"])
    assert out == ""


# ─── handoff-mention detection (2026-05-26 PR #13) ───────────────────
def test_handoff_detector_flags_prose_only_handoff(fake_home, store):  # noqa: F811
    """`alice 可以动了` without @alice → must be flagged."""
    import post_router
    # alice must exist as an employee for the detector to consider her.
    (fake_home / ".openclaw" / "workspace-alice").mkdir(parents=True, exist_ok=True)
    (fake_home / ".openclaw" / "workspace-alice" / "SOUL.md").write_text("# alice")
    (fake_home / ".openclaw" / "workspace-alice" / "IDENTITY.md").write_text(
        "- **Name:** Alice\n- **Emoji:** 🅰️\n"
    )
    (fake_home / ".openclaw" / "agents" / "alice").mkdir(parents=True, exist_ok=True)

    reply = "PR-C2 文案改完了，alice review 完了可以动了。"
    misses = post_router._detect_missing_handoff_mentions(
        reply, mentions=[], speaker="judy",
    )
    assert "alice" in misses


def test_handoff_detector_silent_when_at_mentioned(fake_home, store):  # noqa: F811
    """Same prose but with @alice present → no false positive."""
    import post_router
    (fake_home / ".openclaw" / "workspace-alice").mkdir(parents=True, exist_ok=True)
    (fake_home / ".openclaw" / "workspace-alice" / "SOUL.md").write_text("# alice")
    (fake_home / ".openclaw" / "agents" / "alice").mkdir(parents=True, exist_ok=True)

    reply = "@alice review 完了可以动了"
    misses = post_router._detect_missing_handoff_mentions(
        reply, mentions=["alice"], speaker="judy",
    )
    assert misses == []


def test_handoff_detector_silent_for_speaker_self(fake_home, store):  # noqa: F811
    """Speaker talking about themselves in handoff phrasing isn't a miss."""
    import post_router
    (fake_home / ".openclaw" / "workspace-judy").mkdir(parents=True, exist_ok=True)
    (fake_home / ".openclaw" / "workspace-judy" / "SOUL.md").write_text("# judy")
    (fake_home / ".openclaw" / "agents" / "judy").mkdir(parents=True, exist_ok=True)

    reply = "judy 接下来去写测试"
    misses = post_router._detect_missing_handoff_mentions(
        reply, mentions=[], speaker="judy",
    )
    assert "judy" not in misses


def test_handoff_detector_silent_without_handoff_verb(fake_home, store):  # noqa: F811
    """Naming a teammate without a handoff verb shouldn't trigger."""
    import post_router
    (fake_home / ".openclaw" / "workspace-alice").mkdir(parents=True, exist_ok=True)
    (fake_home / ".openclaw" / "workspace-alice" / "SOUL.md").write_text("# alice")
    (fake_home / ".openclaw" / "agents" / "alice").mkdir(parents=True, exist_ok=True)

    reply = "之前 alice 已经把方案过了一遍。"
    misses = post_router._detect_missing_handoff_mentions(
        reply, mentions=[], speaker="judy",
    )
    # No "可以动" / "交给" / "请你" verb near alice → not a handoff miss.
    assert "alice" not in misses


# ─── orphan placeholder recovery (2026-05-26 PR #15) ─────────────────
def test_recover_orphan_placeholders_supersedes_and_redispatches(
    fake_home, store, monkeypatch  # noqa: F811
):
    """A dangling `__router__` placeholder should be superseded with an
    interrupt note AND the trigger should be re-dispatched.
    """
    import post_router

    # Make sure designer is a known employee so name_to_id can resolve.
    (fake_home / ".openclaw" / "workspace-designer").mkdir(parents=True, exist_ok=True)
    (fake_home / ".openclaw" / "workspace-designer" / "SOUL.md").write_text("# designer")
    (fake_home / ".openclaw" / "workspace-designer" / "IDENTITY.md").write_text(
        "- **Name:** Dora\n- **Emoji:** 🎨\n"
    )
    (fake_home / ".openclaw" / "agents" / "designer").mkdir(parents=True, exist_ok=True)
    (fake_home / ".openclaw" / "agents" / "designer" / "sessions").mkdir(
        parents=True, exist_ok=True
    )

    # Create a thread with a scott @-mention of designer (the trigger), then
    # manually write a __router__ placeholder under it — simulating the
    # state that exists in events.jsonl right when a restart kills the
    # in-flight worker.
    store.ensure_default_squads()
    sq = store.create_squad({
        "id": "rcv", "name": "rcv",
        "members": ["scott", "designer"], "chair": "scott",
    })
    t = store.create_thread(sq["id"], "scott", "@dora design 好了吗")
    trigger = t["posts"][-1]
    trigger_pid = trigger.get("post_id") or trigger["id"]
    # Inject the dangling placeholder directly (skip the normal _dispatch
    # path so no worker actually runs).
    ph = store.add_thread_post(
        t["thread_id"], post_router.ROUTER_SPEAKER_FALLBACK,
        "⏳ @Dora 正在思考中…", parent_post_id=trigger_pid,
    )
    placeholder_id = (ph.get("post_id") or ph["id"])

    # Sanity: projection has the placeholder live and not superseded.
    proj = store.project_thread(t["thread_id"])
    live = [p for p in proj["posts"] if (p.get("post_id") or p.get("id")) == placeholder_id]
    assert live and not live[0]["superseded"]

    # Recover (redispatch=True so the worker actually picks the trigger up).
    with post_router._inflight_lock:
        post_router._inflight.clear()
    results = post_router.recover_orphan_placeholders(redispatch=True)

    # We should have recovered exactly the one placeholder for designer.
    assert any(r["agent_id"] == "designer" for r in results)
    rec = next(r for r in results if r["agent_id"] == "designer")
    assert rec["redispatched"] is True
    assert rec["trigger_pid"] == trigger_pid

    # Projection now shows the placeholder superseded by a note from __router__.
    proj2 = store.project_thread(t["thread_id"])
    live2 = [p for p in proj2["posts"] if (p.get("post_id") or p.get("id")) == placeholder_id]
    assert live2 and live2[0]["superseded"]
    notes = [
        p for p in proj2["posts"]
        if p["speaker"] == post_router.ROUTER_SPEAKER_FALLBACK
        and "重启" in (p.get("content") or "")
    ]
    assert notes, "expected an interrupt note from __router__"

    # Let the re-dispatched worker run to completion so we don't leak it
    # across the test boundary.
    _wait_for(lambda: not post_router._inflight, timeout=6.0)


def test_recover_orphan_placeholders_no_op_when_already_superseded(
    fake_home, store, monkeypatch  # noqa: F811
):
    """Already-resolved placeholders are left alone (no double-recovery)."""
    import post_router

    store.ensure_default_squads()
    sq = store.create_squad({
        "id": "done", "name": "done",
        "members": ["scott", "designer"], "chair": "scott",
    })
    t = store.create_thread(sq["id"], "scott", "@dora ping")
    trigger_pid = (t["posts"][-1].get("post_id") or t["posts"][-1]["id"])
    ph = store.add_thread_post(
        t["thread_id"], post_router.ROUTER_SPEAKER_FALLBACK,
        "⏳ @Dora 正在思考中…", parent_post_id=trigger_pid,
    )
    final = store.add_thread_post(
        t["thread_id"], "designer", "已完成", parent_post_id=trigger_pid,
    )
    store.supersede_thread_post(
        t["thread_id"], (ph.get("post_id") or ph["id"]), by_post_id=(final.get("post_id") or final["id"]),
    )

    results = post_router.recover_orphan_placeholders(redispatch=True)
    assert results == [], "should not touch already-superseded placeholders"


def test_recover_orphan_placeholders_supersedes_unknown_name(
    fake_home, store, monkeypatch  # noqa: F811
):
    """If the placeholder names an unknown agent, still supersede so the
    UI clears — just don't redispatch (we don't know who to dispatch to).
    """
    import post_router

    store.ensure_default_squads()
    sq = store.create_squad({
        "id": "uk", "name": "uk",
        "members": ["scott"], "chair": "scott",
    })
    t = store.create_thread(sq["id"], "scott", "ping")
    trigger_pid = (t["posts"][-1].get("post_id") or t["posts"][-1]["id"])
    ph = store.add_thread_post(
        t["thread_id"], post_router.ROUTER_SPEAKER_FALLBACK,
        "⏳ @NoSuchAgent 正在思考中…", parent_post_id=trigger_pid,
    )
    results = post_router.recover_orphan_placeholders(redispatch=True)
    assert results and results[0]["agent_id"] is None
    assert results[0]["redispatched"] is False

    proj = store.project_thread(t["thread_id"])
    live = [p for p in proj["posts"] if (p.get("post_id") or p.get("id")) == (ph.get("post_id") or ph["id"])]
    assert live and live[0]["superseded"]


# ─── plan-without-action detection (2026-05-26 PR-16-followup) ───────
def test_plan_detector_flags_promise_without_delivery():
    import post_router
    reply = (
        "对，scott 这话戳到了。开发本来就是我的活，前端代码我自己写才对。\n\n"
        "现在我直接开干前端：照搬 mock CSS 进 web/style.css，6 分钟写主体，"
        "5 分钟跨浏览器验。"
    )
    hit = post_router._detect_plan_without_action(reply)
    assert hit == "现在我直接", f"expected promise phrase, got {hit!r}"


def test_plan_detector_silent_when_delivery_present():
    import post_router
    reply = (
        "搞定 scott — rebase + 测试全绿 + force-push 已完成。\n\n"
        "PR #16 已开，commit `acfc89f`。接下来我会继续盯 review。"
    )
    # Has both "我会继续盯" (intent) AND "已完成 / PR #16 已开" (delivery).
    # Delivery wins; don't flag.
    assert post_router._detect_plan_without_action(reply) is None


def test_plan_detector_silent_for_short_replies():
    import post_router
    assert post_router._detect_plan_without_action("好的") is None
    assert post_router._detect_plan_without_action("我马上去做") is None  # <30ch


def test_plan_detector_silent_without_intent_phrase():
    import post_router
    reply = (
        "PR #16 后端已经 rebase 完了，等前端补完一起 merge。"
        "dora 前端还没 push commit。"
    )
    assert post_router._detect_plan_without_action(reply) is None


def test_plan_detector_silent_for_listener_response():
    import post_router
    reply = (
        "scott 这个判断是对的，多空 5:5。如果你坚持持仓，"
        "止损我建议放 0.495。我没有更强的意见。"
    )
    # Pure consultation / answer — no promise to act, no plan-intent verb.
    assert post_router._detect_plan_without_action(reply) is None


# ─── recovery default = no auto-redispatch (PR-17-followup) ──────────
def test_recover_default_supersedes_but_does_not_redispatch(fake_home, store):  # noqa: F811
    """Default recovery (redispatch=False) must clear the UI but NOT
    auto-fire the trigger again. Real incident 2026-05-26 19:37:58:
    auto-redispatch raced with the previous worker's session-file lock
    and produced EmbeddedAttemptSessionTakeoverError."""
    import post_router

    (fake_home / ".openclaw" / "workspace-designer").mkdir(parents=True, exist_ok=True)
    (fake_home / ".openclaw" / "workspace-designer" / "SOUL.md").write_text("# designer")
    (fake_home / ".openclaw" / "workspace-designer" / "IDENTITY.md").write_text(
        "- **Name:** Dora\n- **Emoji:** 🎨\n"
    )
    (fake_home / ".openclaw" / "agents" / "designer").mkdir(parents=True, exist_ok=True)

    store.ensure_default_squads()
    sq = store.create_squad({
        "id": "rcv2", "name": "rcv2",
        "members": ["scott", "designer"], "chair": "scott",
    })
    t = store.create_thread(sq["id"], "scott", "@dora ping again")
    trigger_pid = t["posts"][-1].get("post_id") or t["posts"][-1]["id"]
    ph = store.add_thread_post(
        t["thread_id"], post_router.ROUTER_SPEAKER_FALLBACK,
        "⏳ @Dora 正在思考中…", parent_post_id=trigger_pid,
    )
    placeholder_id = ph.get("post_id") or ph["id"]

    with post_router._inflight_lock:
        post_router._inflight.clear()
    # DEFAULT call — redispatch should be False.
    results = post_router.recover_orphan_placeholders()

    rec = next(r for r in results if r["agent_id"] == "designer")
    assert rec["redispatched"] is False, "default must NOT redispatch"

    # Placeholder is superseded (UI clears).
    proj = store.project_thread(t["thread_id"])
    live = [
        p for p in proj["posts"]
        if (p.get("post_id") or p.get("id")) == placeholder_id
    ]
    assert live and live[0]["superseded"]

    # The interrupt note mentions the manual-@ guidance.
    notes = [
        p for p in proj["posts"]
        if p["speaker"] == post_router.ROUTER_SPEAKER_FALLBACK
        and "人工 @" in (p.get("content") or "")
    ]
    assert notes, "expected the manual-@ guidance note"

    # No worker should be in flight (because we did not redispatch).
    assert not post_router._inflight, "default recovery must not spawn a worker"


# ─── graceful shutdown (PR-21) ───────────────────────────────────────
def test_drain_blocks_new_dispatches(fake_home, store):  # noqa: F811
    """Once drain_and_terminate has been called, enqueue_if_needed must
    refuse to spawn any new worker — those triggers will be picked up
    as orphan placeholders on the next boot."""
    import post_router
    # Drain immediately (no active procs to terminate).
    n = post_router.drain_and_terminate(grace_seconds=0.1)
    assert n == 0
    assert post_router.is_draining()

    store.ensure_default_squads()
    sq = store.create_squad({
        "id": "drn", "name": "drn",
        "members": ["scott", "milk"], "chair": "scott",
    })
    t = store.create_thread(sq["id"], "scott", "@milk hi")
    fake_post = {
        "speaker": "scott",
        "content": "@milk ping",
        "mentions": ["milk"],
        "parent_post_id": None,
        "post_id": "p_test",
    }
    dispatched = post_router.enqueue_if_needed(t["thread_id"], fake_post)
    assert dispatched is False, "draining server must not dispatch"


def test_terminate_all_active_sigterms_registered_procs(fake_home, monkeypatch, tmp_path):  # noqa: F811
    """terminate_all_active should SIGTERM every registered Popen and
    return the count. We register fake long-running shell subprocesses
    and verify they actually die."""
    import os
    import subprocess
    import time

    import agent_runtime

    # Drop any leftover entries from earlier tests.
    with agent_runtime._active_procs_lock:
        agent_runtime._active_procs.clear()

    procs = []
    for _ in range(3):
        # Each child sleeps a long time in its own process group.
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        procs.append(proc)
        agent_runtime._register_active(proc.pid, proc)

    assert agent_runtime.active_count() == 3

    killed = agent_runtime.terminate_all_active(grace_seconds=2.0)
    assert killed == 3

    # Within a generous deadline, all three should be reaped.
    deadline = time.time() + 4.0
    while time.time() < deadline:
        alive = [p for p in procs if p.poll() is None]
        if not alive:
            break
        time.sleep(0.1)
    survivors = [p.pid for p in procs if p.poll() is None]
    if survivors:
        # Force-cleanup so we don't leak across tests.
        for p in procs:
            try:
                os.killpg(p.pid, 9)
            except ProcessLookupError:
                pass
        raise AssertionError(
            f"terminate_all_active left {len(survivors)} survivors: {survivors}"
        )

    # Always clear regardless so other tests start fresh.
    with agent_runtime._active_procs_lock:
        agent_runtime._active_procs.clear()


def test_drain_idempotent(fake_home):  # noqa: F811
    """Calling drain twice is fine and stays draining."""
    import post_router
    post_router.drain_and_terminate(grace_seconds=0.1)
    assert post_router.is_draining()
    n = post_router.drain_and_terminate(grace_seconds=0.1)
    assert n == 0
    assert post_router.is_draining()
