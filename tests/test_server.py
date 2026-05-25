"""End-to-end HTTP smoke test: boot server, hit API, assert behaviour."""
# ruff: noqa: F811  (pytest fixture re-export shadows the import)
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Re-export the shared `server` fixture for tests that import it from here.
from tests.conftest import server  # noqa: F401, E402  (pytest fixture re-export)


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-OpenForge-UI": "1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # surface server error body so test failures are debuggable
        body_text = e.read().decode("utf-8", errors="replace")
        raise AssertionError(f"POST {url} -> {e.code}: {body_text}") from None


def test_squads_starts_empty(server):
    assert _get(f"{server}/api/squads") == []


def test_full_thread_lifecycle(server):
    # 1. create a squad
    sq = _post(f"{server}/api/squads", {
        "id": "milk_eng",
        "name": "milk_eng",
        "members": ["milk", "sentry"],
        "chair": "milk",
    })
    assert sq["id"] == "milk_eng"

    # 2. create a thread + opening post
    th = _post(f"{server}/api/squads/milk_eng/threads", {
        "content": "kick off no-at",
        "created_by": "scott",
    })
    tid = th["thread_id"]
    assert th["post_count"] == 1

    # 3. add a follow-up post
    _post(f"{server}/api/threads/{tid}/posts", {
        "content": "second message",
        "speaker": "scott",
    })

    # 4. fetch thread, expect 2 live human/agent posts (filter out the
    # router placeholders V1.1 default-to-chair adds for naked scott posts).
    detail = _get(f"{server}/api/threads/{tid}")
    live = [p for p in detail["posts"]
            if not p["superseded"] and p["speaker"] != "__router__"]
    assert len(live) == 2

    # 5. add a reaction; expect projection to surface it
    pid = live[0]["id"]
    out = _post(f"{server}/api/threads/{tid}/posts/{pid}/reactions", {
        "emoji": "👍", "actor": "scott",
    })
    assert out["reactions"] == {"👍": ["scott"]}

    detail2 = _get(f"{server}/api/threads/{tid}")
    p0 = next(p for p in detail2["posts"] if p["id"] == pid)
    assert p0["reactions"] == {"👍": ["scott"]}


def test_bad_emoji_rejected(server):
    _post(f"{server}/api/squads", {
        "id": "x", "name": "x", "members": ["m"], "chair": "m",
    })
    t = _post(f"{server}/api/squads/x/threads", {"content": "hi", "created_by": "scott"})
    pid = t["posts"][0]["id"]
    with pytest.raises(AssertionError, match="400"):
        _post(f"{server}/api/threads/{t['thread_id']}/posts/{pid}/reactions", {"emoji": ""})


def test_session_search_endpoint(server, fake_home):
    """v0.9.2: GET /api/agents/<id>/session-search end-to-end."""
    import json as _json
    import os as _os
    import time as _time
    from datetime import UTC
    from datetime import datetime as _dt

    sess_dir = Path(fake_home) / ".openclaw" / "agents" / "miki" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    now = _time.time()
    iso = _dt.fromtimestamp(now - 30, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    line = _json.dumps({
        "type": "message",
        "timestamp": iso,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "mustafa email archive done"}],
        },
    })
    (sess_dir / "main-1.jsonl").write_text(line + "\n")

    # Hit count.
    res = _get(f"{server}/api/agents/miki/session-search?q=mustafa&days=30")
    assert res["agent"] == "miki"
    assert res["total_hits"] >= 1
    assert res["hits"][0]["session_id"] == "main-1"
    assert "mustafa" in res["hits"][0]["snippet"].lower()
    # Default scope == main.
    assert res["scope"] == "main"

    # scope=forge → no hits (the test file has no forge prefix).
    res_f = _get(f"{server}/api/agents/miki/session-search?q=mustafa&scope=forge")
    assert res_f["total_hits"] == 0

    # Bad days → 400.
    with pytest.raises(AssertionError, match="400"):
        try:
            _get(f"{server}/api/agents/miki/session-search?q=mustafa&days=bogus")
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise AssertionError(f"GET -> {e.code}: {body_text}") from None

    # Empty query → 400.
    with pytest.raises(AssertionError, match="400"):
        try:
            _get(f"{server}/api/agents/miki/session-search?q=")
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise AssertionError(f"GET -> {e.code}: {body_text}") from None

    # Bad scope → 400.
    with pytest.raises(AssertionError, match="400"):
        try:
            _get(f"{server}/api/agents/miki/session-search?q=x&scope=lolwhat")
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise AssertionError(f"GET -> {e.code}: {body_text}") from None
    _ = _os  # silence linter


# ─── speaker spoofing guard (2026-05-25 incident regression) ──────────
def _raw_post(url: str, body: dict, headers: dict | None = None):
    """POST without any test-rig defaults; returns (status, json|err)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body_text)
        except json.JSONDecodeError:
            return e.code, {"_raw": body_text}


def _bootstrap_squad_and_thread(server):
    """Create a squad + thread we can post into; uses UI header so allowed."""
    _post(f"{server}/api/squads", {
        "id": "spoof-test", "name": "spoof-test",
        "members": ["scott", "designer"], "chair": "scott",
    })
    thread = _post(f"{server}/api/squads/spoof-test/threads", {
        "content": "opener", "created_by": "scott",
    })
    return thread["thread_id"]


def test_post_rejects_missing_speaker(server):
    tid = _bootstrap_squad_and_thread(server)
    code, body = _raw_post(
        f"{server}/api/threads/{tid}/posts",
        {"content": "no speaker"},
    )
    assert code == 400
    assert "speaker" in (body.get("error") or "")


def test_post_rejects_empty_speaker(server):
    tid = _bootstrap_squad_and_thread(server)
    code, body = _raw_post(
        f"{server}/api/threads/{tid}/posts",
        {"content": "blank", "speaker": "   "},
    )
    assert code == 400


def test_post_rejects_scott_without_ui_header(server):
    """Agent curling loopback cannot impersonate scott — the 2026-05-25 bug."""
    tid = _bootstrap_squad_and_thread(server)
    code, body = _raw_post(
        f"{server}/api/threads/{tid}/posts",
        {"content": "fake CEO post", "speaker": "scott"},
    )
    assert code == 403
    assert "scott" in (body.get("error") or "").lower()
    # Even mixed case shouldn't slip through.
    code2, _ = _raw_post(
        f"{server}/api/threads/{tid}/posts",
        {"content": "still fake", "speaker": "ScOtT"},
    )
    assert code2 == 403


def test_post_allows_scott_with_ui_header(server):
    tid = _bootstrap_squad_and_thread(server)
    code, body = _raw_post(
        f"{server}/api/threads/{tid}/posts",
        {"content": "real CEO post", "speaker": "scott"},
        headers={"X-OpenForge-UI": "1"},
    )
    assert code == 201
    posts = body.get("posts") or []
    assert any(
        p["speaker"] == "scott" and p["content"] == "real CEO post"
        for p in posts
    )


def test_post_allows_agent_speaker_without_ui_header(server):
    """Agents post as themselves over loopback — that's the normal path."""
    tid = _bootstrap_squad_and_thread(server)
    code, body = _raw_post(
        f"{server}/api/threads/{tid}/posts",
        {"content": "designer reply", "speaker": "designer"},
    )
    assert code == 201
    posts = body.get("posts") or []
    assert any(p["speaker"] == "designer" for p in posts)


def test_post_rejects_router_speaker(server):
    tid = _bootstrap_squad_and_thread(server)
    code, _ = _raw_post(
        f"{server}/api/threads/{tid}/posts",
        {"content": "spoof router", "speaker": "__router__"},
    )
    assert code == 400


def test_create_thread_rejects_scott_without_ui_header(server):
    """Same guard applies to thread creation's `created_by`."""
    _post(f"{server}/api/squads", {
        "id": "spoof-create", "name": "spoof-create",
        "members": ["scott"], "chair": "scott",
    })
    code, body = _raw_post(
        f"{server}/api/squads/spoof-create/threads",
        {"content": "fake opener", "created_by": "scott"},
    )
    assert code == 403
