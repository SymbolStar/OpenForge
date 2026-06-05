"""HTTP smoke tests for v0.10 thread-pin endpoints."""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error

from tests.test_server import _get, _post  # noqa: F401
from tests.conftest import server  # noqa: F401


def _http(method, url, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "X-OpenForge-UI": "1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            body = {}
        return e.code, body


def _mk_thread(server):
    sid = f"pin{os.getpid()}{abs(hash(os.urandom(4))) % 100000}"
    sq = _post(f"{server}/api/squads", {"id": sid, "name": sid,
                                        "members": ["scott"], "chair": "scott"})
    t = _post(f"{server}/api/squads/{sq['id']}/threads", {"content": "open", "created_by": "scott"})
    return t["thread_id"]


def _mk_ref(server, tmp_path, label="x.md"):
    f = tmp_path / label
    f.write_text("hi")
    code, ref = _http("POST", f"{server}/api/refs", {
        "label": label,
        "abs_path": str(f),
        "source_agent": "bugfix",
    })
    assert code == 201, ref
    return ref["id"], f


def test_pin_and_list(server, tmp_path):
    tid = _mk_thread(server)
    rid, _ = _mk_ref(server, tmp_path)
    code, resp = _http("POST", f"{server}/api/threads/{tid}/pinned-refs",
                       {"ref_id": rid, "actor": "scott"})
    assert code == 201, resp
    assert resp["pinned_refs"][0]["ref_id"] == rid

    code, resp = _http("GET", f"{server}/api/threads/{tid}/pinned-refs")
    assert code == 200
    assert resp["cap"] == 5
    assert resp["pinned_refs"][0]["pinned_by"] == "scott"


def test_pin_cap_returns_409(server, tmp_path):
    tid = _mk_thread(server)
    rids = []
    for i in range(5):
        rid, _ = _mk_ref(server, tmp_path, f"f{i}.md")
        rids.append(rid)
        code, _ = _http("POST", f"{server}/api/threads/{tid}/pinned-refs",
                        {"ref_id": rid, "actor": "scott"})
        assert code == 201
    rid_extra, _ = _mk_ref(server, tmp_path, "extra.md")
    code, resp = _http("POST", f"{server}/api/threads/{tid}/pinned-refs",
                       {"ref_id": rid_extra, "actor": "scott"})
    assert code == 409
    assert resp["error"] == "PIN_CAP_REACHED"


def test_unpin_emits_system_post(server, tmp_path):
    tid = _mk_thread(server)
    rid, _ = _mk_ref(server, tmp_path, "PLAN.md")
    _http("POST", f"{server}/api/threads/{tid}/pinned-refs",
          {"ref_id": rid, "actor": "scott"})
    code, _ = _http("DELETE",
                    f"{server}/api/threads/{tid}/pinned-refs/{rid}?actor=dora&label=PLAN.md")
    assert code == 200
    t = _get(f"{server}/api/threads/{tid}")
    sys_posts = [p for p in t["posts"] if p["speaker"] == "__system__"]
    assert sys_posts and "PLAN.md" in sys_posts[-1]["content"]


def test_refs_exists_single_and_batch(server, tmp_path):
    rid, f = _mk_ref(server, tmp_path, "alive.md")
    code, resp = _http("GET", f"{server}/api/refs/{rid}/exists")
    assert code == 200 and resp["exists"] is True
    f.unlink()
    code, resp = _http("GET", f"{server}/api/refs/{rid}/exists")
    assert code == 200 and resp["exists"] is False

    rid2, _ = _mk_ref(server, tmp_path, "also-alive.md")
    code, resp = _http("POST", f"{server}/api/refs/exists",
                       {"ids": [rid, rid2, "ref_nope"]})
    assert code == 200
    assert resp["results"][rid] is False
    assert resp["results"][rid2] is True
    assert resp["results"]["ref_nope"] is False
