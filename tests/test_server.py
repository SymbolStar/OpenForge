"""End-to-end HTTP smoke test: boot server, hit API, assert behaviour."""
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
        headers={"Content-Type": "application/json"},
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

    # 4. fetch thread, expect 2 live posts
    detail = _get(f"{server}/api/threads/{tid}")
    live = [p for p in detail["posts"] if not p["superseded"]]
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
