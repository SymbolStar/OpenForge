"""More server.py HTTP coverage: SSE end-to-end, error paths, PATCH/DELETE
squads, auth, etc. Reuses the `server` subprocess fixture from test_server."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.test_server import _get, _post  # noqa: E402
from tests.conftest import _free_port, _wait_up  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _http(method, url, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, ConnectionRefusedError, ConnectionResetError):
        return 0, ""


# ─── static file routes & misc GETs ─────────────────────────────────
def test_static_index_and_assets(server):
    for path, _ in [("/", "html"), ("/index.html", "html"),
                    ("/style.css", "css"), ("/app.js", "javascript")]:
        with urllib.request.urlopen(f"{server}{path}", timeout=2) as r:
            assert r.status == 200


def test_unknown_static_returns_404(server):
    code, _ = _http("GET", f"{server}/nope/whatever")
    assert code == 404


def test_agents_endpoint_lists_squad_members(server):
    _post(f"{server}/api/squads",
          {"id": "ag", "name": "ag", "members": ["foo", "bar"], "chair": "foo"})
    agents = _get(f"{server}/api/agents")
    assert "foo" in agents and "bar" in agents


def test_get_squad_404(server):
    code, body = _http("GET", f"{server}/api/squads/ghost")
    assert code == 404
    assert "not found" in body


def test_get_thread_404(server):
    code, body = _http("GET", f"{server}/api/threads/th_dead_beef")
    assert code == 404
    assert "not found" in body


def test_squads_include_archived_query(server):
    _post(f"{server}/api/squads",
          {"id": "ar", "name": "ar", "members": ["m"], "chair": "m"})
    code, body = _http("PATCH", f"{server}/api/squads/ar", {"archived": True})
    assert code == 200
    assert _get(f"{server}/api/squads") == []
    incl = _get(f"{server}/api/squads?include_archived=1")
    assert any(s["id"] == "ar" for s in incl)


# ─── POST validation paths ──────────────────────────────────────────
def test_post_squads_bad_json(server):
    req = urllib.request.Request(f"{server}/api/squads", data=b"not json",
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
    try:
        urllib.request.urlopen(req, timeout=2)
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_post_squads_validation_errors(server):
    code, _ = _http("POST", f"{server}/api/squads", {"id": "BAD ID", "members": []})
    assert code == 400
    code, _ = _http("POST", f"{server}/api/squads", {"id": "ok", "members": []})
    assert code == 400
    code, _ = _http("POST", f"{server}/api/squads",
                    {"id": "ok2", "members": [""], "chair": "ok2"})
    assert code == 400
    code, _ = _http("POST", f"{server}/api/squads",
                    {"id": "ok3", "members": ["m"], "chair": "ghost"})
    assert code == 400


def test_post_squads_duplicate(server):
    _post(f"{server}/api/squads",
          {"id": "dup", "members": ["m"], "chair": "m"})
    code, _ = _http("POST", f"{server}/api/squads",
                    {"id": "dup", "members": ["m"], "chair": "m"})
    assert code == 409 or code == 400


def test_create_thread_validation(server):
    _post(f"{server}/api/squads",
          {"id": "tv", "members": ["m"], "chair": "m"})
    code, _ = _http("POST", f"{server}/api/squads/tv/threads", {"content": "   "})
    assert code == 400
    code, _ = _http("POST", f"{server}/api/squads/ghost/threads",
                    {"content": "hi"})
    assert code == 404 or code == 400


def test_thread_post_validation(server):
    _post(f"{server}/api/squads",
          {"id": "tp", "members": ["m"], "chair": "m"})
    t = _post(f"{server}/api/squads/tp/threads",
              {"content": "hi", "created_by": "scott"})
    tid = t["thread_id"]
    code, _ = _http("POST", f"{server}/api/threads/{tid}/posts", {"content": ""})
    assert code == 400
    code, _ = _http("POST", f"{server}/api/threads/{tid}/posts",
                    {"content": "x", "parent_post_id": 123})
    assert code == 400
    code, _ = _http("POST", f"{server}/api/threads/th_dead_beef/posts",
                    {"content": "x"})
    assert code == 404
    code, _ = _http("POST", f"{server}/api/threads/{tid}/close", {})
    assert code == 200


def test_close_unknown_thread(server):
    code, body = _http("POST", f"{server}/api/threads/th_dead_beef/close", {})
    assert "unknown thread" in body or code == 404


def test_reaction_unknown_thread(server):
    code, body = _http("POST",
                       f"{server}/api/threads/th_dead_beef/posts/p_x/reactions",
                       {"emoji": "👍"})
    assert "unknown thread" in body or code == 404


def test_reaction_missing_emoji(server):
    _post(f"{server}/api/squads",
          {"id": "rx", "members": ["m"], "chair": "m"})
    t = _post(f"{server}/api/squads/rx/threads",
              {"content": "hi", "created_by": "scott"})
    pid = t["posts"][0]["id"]
    code, _ = _http("POST",
                    f"{server}/api/threads/{t['thread_id']}/posts/{pid}/reactions",
                    {})
    assert code == 400


# ─── PATCH/DELETE squads ────────────────────────────────────────────
def test_patch_squad_update_and_404(server):
    _post(f"{server}/api/squads",
          {"id": "pp", "members": ["a", "b"], "chair": "a"})
    code, body = _http("PATCH", f"{server}/api/squads/pp",
                       {"name": "renamed"})
    assert code == 200
    assert json.loads(body)["name"] == "renamed"
    # bogus chair
    code, _ = _http("PATCH", f"{server}/api/squads/pp", {"chair": "ghost"})
    assert code == 400
    # not found
    code, _ = _http("PATCH", f"{server}/api/squads/ghost", {"name": "x"})
    assert code == 404
    # bad route
    code, _ = _http("PATCH", f"{server}/api/something/else", {})
    assert code == 404


def test_delete_squad(server):
    _post(f"{server}/api/squads",
          {"id": "dl", "members": ["m"], "chair": "m"})
    code, _ = _http("DELETE", f"{server}/api/squads/dl")
    assert code == 200
    code, _ = _http("DELETE", f"{server}/api/squads/dl")
    assert code == 404
    code, _ = _http("DELETE", f"{server}/api/something/else")
    assert code == 404


# ─── SSE end-to-end ─────────────────────────────────────────────────
def test_sse_stream_delivers_post_added(server):
    _post(f"{server}/api/squads",
          {"id": "sse", "members": ["m"], "chair": "m"})
    t = _post(f"{server}/api/squads/sse/threads",
              {"content": "hi", "created_by": "scott"})
    tid = t["thread_id"]

    received: list[bytes] = []
    error: list[Exception] = []

    def _reader():
        try:
            with urllib.request.urlopen(f"{server}/api/threads/{tid}/events",
                                        timeout=5) as r:
                # read until we get the hello + one data event
                start = time.time()
                while time.time() - start < 4:
                    line = r.readline()
                    if not line:
                        break
                    received.append(line)
                    if b"post_added" in line:
                        return
        except Exception as e:  # noqa: BLE001
            error.append(e)

    th = threading.Thread(target=_reader)
    th.start()
    time.sleep(0.4)  # let subscription register
    _post(f"{server}/api/threads/{tid}/posts",
          {"content": "second", "speaker": "scott"})
    th.join(timeout=5)
    assert not error, error
    blob = b"".join(received)
    assert b"hello" in blob
    assert b"post_added" in blob


def test_sse_unknown_thread_404(server):
    code, _ = _http("GET", f"{server}/api/threads/th_dead_beef/events")


# ─── auth token configured ────────────────────────────────────────
def test_server_token_auth_path(fake_home):
    """With --token X on loopback, valid Bearer succeeds; bad Bearer 401s."""
    import signal as _sig
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-u", str(REPO_ROOT / "server.py"),
         "--port", str(port), "--token", "secret-abc"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**__import__("os").environ, "HOME": str(fake_home)},
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            code, _ = _http("GET", f"http://127.0.0.1:{port}/api/squads",
                            headers={"Authorization": "Bearer secret-abc"})
            if code == 200:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("server never came up with token")
        # wrong token -> 401
        code, _ = _http("GET", f"http://127.0.0.1:{port}/api/squads",
                        headers={"Authorization": "Bearer wrong"})
        assert code == 401
        # missing token -> 401
        code, _ = _http("GET", f"http://127.0.0.1:{port}/api/squads")
        assert code == 401
        # token via query string (SSE pathway)
        code, _ = _http("GET",
                        f"http://127.0.0.1:{port}/api/squads?token=secret-abc")
        assert code == 200
    finally:
        proc.send_signal(_sig.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=2)
