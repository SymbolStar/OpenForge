"""End-to-end HTTP smoke test: boot server, hit API, assert behaviour."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_up(url: str, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return
        except (urllib.error.URLError, ConnectionResetError, ConnectionRefusedError):
            time.sleep(0.1)
    raise RuntimeError(f"server did not come up at {url}")


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


@pytest.fixture
def server(fake_home):
    """Boot server.py against a fake $HOME on a random port."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-u", str(REPO_ROOT / "server.py"), "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**__import__("os").environ, "HOME": str(fake_home)},
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_up(f"{base}/api/squads")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


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
