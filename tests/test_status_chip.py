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
    # Chip post is authored by __router__ but carries the target agent_id
    # so the UI can render the chip with the agent's avatar/name.
    assert chip["speaker"] == "__router__"
    assert chip["agent_id"] == "milk"
    reply = next(p for p in proj["posts"] if p["speaker"] == "milk" and p["content"] == "done reply")
    # PR 20:18: reply post is tagged with from_chip_post_id pointing at
    # the chip so the UI can suppress the done chip and inline duration
    # on the reply header.
    assert reply["from_chip_post_id"] == chip["id"]
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


def test_from_chip_post_id_in_wire_payload(server):
    """Regression guard for PR #31 follow-up: dora caught that the field
    was in events + projection but missing from _serializable_post, so
    `GET /api/threads/:tid` returned null and the front-end chip→reply
    pairing silently failed. Assert the field round-trips on the wire.
    """
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
    store.add_thread_post(tid, "milk", "real reply", from_chip_post_id=chip["post_id"])
    with _u.urlopen(f"{server}/api/threads/{tid}", timeout=2) as r:
        wire = json.loads(r.read().decode("utf-8"))
    reply = next(p for p in wire["posts"] if p["speaker"] == "milk")
    assert reply["from_chip_post_id"] == chip["post_id"]


def test_cancel_endpoint_marks_chip_and_audits(store, monkeypatch):
    """POST /posts/<chip>/cancel: chip flips to 'cancelled', router marks
    the chip so a late reply is dropped, an audit __router__ post is added,
    and a best-effort SIGTERM is sent to the bound subprocess group."""
    import sys
    sys.modules.pop("server", None)
    import server as srv
    import post_router

    t = _make_thread(store)
    chip = store.add_thread_post(
        t["thread_id"], "__router__", "milk thinking",
        post_type="status_chip", phase="thinking",
        trigger_post_id=t["posts"][0]["id"], agent_id="milk",
    )

    sigterm_calls = []
    monkeypatch.setattr(
        "agent_runtime.cancel_chip_subprocess",
        lambda pid: sigterm_calls.append(("native", pid)) or True,
    )
    monkeypatch.setattr(
        "acp_runtime.cancel_acp_chip_subprocess",
        lambda pid: sigterm_calls.append(("acp", pid)) or False,
    )

    handler = object.__new__(srv.OpenForgeHandler)
    handler.path = f"/api/threads/{t['thread_id']}/posts/{chip['post_id']}/cancel"
    handler.headers = {}
    handler.rfile = io.BytesIO(b"{}")
    out = {}
    monkeypatch.setattr(handler, "_check_auth", lambda: True)
    monkeypatch.setattr(handler, "_json", lambda obj, status=200, extra_headers=None: out.update(status=status, obj=obj))
    handler.do_POST()

    assert out["status"] == 200
    assert out["obj"]["phase"] == "cancelled"
    assert post_router.is_cancelled(chip["post_id"])
    # Both cancel hooks invoked best-effort.
    kinds = {k for k, _ in sigterm_calls}
    assert kinds == {"native", "acp"}
    # Audit post landed.
    proj = store.project_thread(t["thread_id"])
    audits = [p for p in proj["posts"]
              if p.get("speaker") == "__router__"
              and p.get("post_type") != "status_chip"
              and "已被中断" in (p.get("content") or "")]
    assert len(audits) == 1


def test_cancel_endpoint_idempotent_on_already_cancelled(store, monkeypatch):
    import sys
    sys.modules.pop("server", None)
    import server as srv
    t = _make_thread(store)
    chip = store.add_thread_post(
        t["thread_id"], "__router__", "milk thinking",
        post_type="status_chip", phase="cancelled",
        trigger_post_id=t["posts"][0]["id"], agent_id="milk",
    )
    handler = object.__new__(srv.OpenForgeHandler)
    handler.path = f"/api/threads/{t['thread_id']}/posts/{chip['post_id']}/cancel"
    handler.headers = {}
    handler.rfile = io.BytesIO(b"{}")
    out = {}
    monkeypatch.setattr(handler, "_check_auth", lambda: True)
    monkeypatch.setattr(handler, "_json", lambda obj, status=200, extra_headers=None: out.update(status=status, obj=obj))
    handler.do_POST()
    assert out["status"] == 200
    assert out["obj"]["phase"] == "cancelled"


def test_cancel_endpoint_rejects_terminal_phase(store, monkeypatch):
    import sys
    sys.modules.pop("server", None)
    import server as srv
    t = _make_thread(store)
    chip = store.add_thread_post(
        t["thread_id"], "__router__", "milk thinking",
        post_type="status_chip", phase="done",
        trigger_post_id=t["posts"][0]["id"], agent_id="milk",
    )
    handler = object.__new__(srv.OpenForgeHandler)
    handler.path = f"/api/threads/{t['thread_id']}/posts/{chip['post_id']}/cancel"
    handler.headers = {}
    handler.rfile = io.BytesIO(b"{}")
    out = {}
    monkeypatch.setattr(handler, "_check_auth", lambda: True)
    monkeypatch.setattr(handler, "_json", lambda obj, status=200, extra_headers=None: out.update(status=status, obj=obj))
    handler.do_POST()
    assert out["status"] == 409
    assert out["obj"]["error"] == "already_completed"
