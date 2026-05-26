"""Status-chip post lifecycle tests."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest


def _make_thread(store, content="@milk help"):
    store.ensure_default_squads()
    if not store.get_squad("sq"):
        store.create_squad({"id": "sq", "name": "sq", "members": ["milk", "scott"], "chair": "scott"})
    return store.create_thread("sq", "scott", content)


def _patch(url: str, body: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"_raw": raw}


def test_patch_post_appends_event(store):
    t = _make_thread(store)
    chip = store.add_thread_post(
        t["thread_id"], "__router__", "milk thinking",
        post_type="status_chip", phase="thinking",
        trigger_post_id=t["posts"][0]["id"],
    )
    updated = store.patch_post(t["thread_id"], chip["post_id"], {"phase": "running"})
    assert updated["phase"] == "running"
    events = store.read_thread_events(t["thread_id"])
    assert events[-1]["kind"] == "post_updated"
    assert events[-1]["post_id"] == chip["post_id"]
    assert events[-1]["patch"] == {"phase": "running"}


def test_phase_lifecycle_thinking_to_done(router, store, monkeypatch):
    monkeypatch.setattr(router, "call_agent", lambda ag, sid, prompt, **_kw: "done reply")
    monkeypatch.setattr(router, "snapshot_main", lambda ag: None)
    t = _make_thread(store)
    trigger = {"post_id": t["posts"][0]["id"], "speaker": "scott", "content": "@milk help", "mentions": ["milk"]}
    router._route_to_agent(t["thread_id"], "milk", trigger)
    proj = store.project_thread(t["thread_id"])
    chip = next(p for p in proj["posts"] if p.get("post_type") == "status_chip")
    assert chip["phase"] == "done"
    assert chip["trigger_post_id"] == t["posts"][0]["id"]
    assert chip["duration_ms"] is not None
    assert any(p["speaker"] == "milk" and p["content"] == "done reply" for p in proj["posts"])


def test_phase_failed_carries_error(router, store, monkeypatch):
    def boom(*_a, **_kw):
        from agent_runtime import AgentError
        raise AgentError("stderr tail kaboom")

    monkeypatch.setattr(router, "call_agent", boom)
    monkeypatch.setattr(router, "snapshot_main", lambda ag: None)
    t = _make_thread(store)
    trigger = {"post_id": t["posts"][0]["id"], "speaker": "scott", "content": "@milk help", "mentions": ["milk"]}
    router._route_to_agent(t["thread_id"], "milk", trigger)
    proj = store.project_thread(t["thread_id"])
    chips = [p for p in proj["posts"] if p.get("post_type") == "status_chip"]
    assert len(chips) == 1
    assert chips[0]["phase"] == "failed"
    assert "kaboom" in chips[0]["error"]
    assert not any(p["speaker"] == "__router__" and p.get("post_type") != "status_chip" for p in proj["posts"])


def test_skip_three_paths_normalize(router, store, monkeypatch):
    monkeypatch.setattr(router, "snapshot_main", lambda ag: None)

    monkeypatch.setattr(router, "call_agent", lambda *_a, **_kw: "completed")
    t1 = _make_thread(store)
    router._route_to_agent(t1["thread_id"], "milk", {"post_id": t1["posts"][0]["id"], "speaker": "scott", "content": "@milk help", "mentions": ["milk"]})
    chip1 = next(p for p in store.project_thread(t1["thread_id"])["posts"] if p.get("post_type") == "status_chip")
    assert chip1["phase"] == "skipped"
    assert "reason" not in chip1

    t2 = _make_thread(store, "@milk closed")
    chip2 = store.add_thread_post(t2["thread_id"], "__router__", "milk thinking", post_type="status_chip", phase="thinking")
    store.close_thread(t2["thread_id"], closed_by="scott")
    router._route_to_agent_safely(t2["thread_id"], "milk", t2["posts"][0]["id"], chip_post_id=chip2["post_id"])
    patched2 = store.project_thread(t2["thread_id"])["posts_by_id"][chip2["post_id"]]
    assert patched2["phase"] == "skipped"
    assert "reason" not in patched2

    t3 = _make_thread(store, "no mention")
    chip3 = store.add_thread_post(t3["thread_id"], "__router__", "milk thinking", post_type="status_chip", phase="thinking")
    router._route_to_agent_safely(t3["thread_id"], "milk", "p_missing", chip_post_id=chip3["post_id"])
    patched3 = store.project_thread(t3["thread_id"])["posts_by_id"][chip3["post_id"]]
    assert patched3["phase"] == "skipped"
    assert "reason" not in patched3


def test_patch_endpoint_validates_phase(server):
    import urllib.request as _u

    def post(url: str, body: dict):
        req = _u.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-OpenForge-UI": "1"},
            method="POST",
        )
        with _u.urlopen(req, timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))

    post(f"{server}/api/squads", {"id": "sq", "name": "sq", "members": ["scott"], "chair": "scott"})
    thread = post(f"{server}/api/squads/sq/threads", {"content": "hello", "created_by": "scott"})
    tid = thread["thread_id"]
    import forge_store as store
    chip = store.add_thread_post(tid, "__router__", "milk thinking", post_type="status_chip", phase="thinking")
    code, body = _patch(f"{server}/api/threads/{tid}/posts/{chip['post_id']}", {"phase": "bogus"})
    assert code == 400
    assert "phase" in body["error"]


def test_retry_endpoint_redispatches(store, monkeypatch):
    import server as srv

    t = _make_thread(store)
    chip = store.add_thread_post(
        t["thread_id"], "__router__", "milk thinking",
        post_type="status_chip", phase="failed",
        trigger_post_id=t["posts"][0]["id"], error="boom",
    )
    calls = []
    monkeypatch.setattr(srv.post_router, "_dispatch", lambda tid, ag, trig, chip_post_id=None: calls.append((tid, ag, trig, chip_post_id)) or True)

    handler = object.__new__(srv.OpenForgeHandler)
    handler.path = f"/api/threads/{t['thread_id']}/posts/{chip['post_id']}/retry"
    handler.headers = {}
    handler.rfile = io.BytesIO(b"{}")
    out = {}
    monkeypatch.setattr(handler, "_check_auth", lambda: True)
    monkeypatch.setattr(handler, "_json", lambda obj, status=200, extra_headers=None: out.update(status=status, obj=obj))
    handler.do_POST()

    assert out["status"] == 200
    assert out["obj"]["phase"] == "thinking"
    assert calls == [(t["thread_id"], "milk", t["posts"][0]["id"], chip["post_id"])]
