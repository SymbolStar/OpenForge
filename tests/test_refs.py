"""Tests for forge_refs module + /api/refs HTTP routes."""
# ruff: noqa: F811
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

pytest_plugins = ["tests.test_server"]


# ─── unit tests ────────────────────────────────────────────────────────


@pytest.fixture
def tmp_md(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("# hello\nfrom milk\n", encoding="utf-8")
    return p


def test_register_then_get(fake_home, tmp_md):
    import forge_refs
    ref = forge_refs.register(
        label="note.md", abs_path=str(tmp_md), source_agent="milk",
    )
    assert ref["id"].startswith("ref_")
    assert ref["label"] == "note.md"
    assert ref["source_agent"] == "milk"
    assert ref["size_hint"] > 0
    got = forge_refs.get_ref(ref["id"])
    assert got and got["abs_path"] == str(tmp_md.resolve())


def test_register_idempotent(fake_home, tmp_md):
    import forge_refs
    a = forge_refs.register(label="a.md", abs_path=str(tmp_md), source_agent="milk")
    b = forge_refs.register(label="other-label.md", abs_path=str(tmp_md), source_agent="milk")
    assert a["id"] == b["id"]


def test_register_different_agents_distinct(fake_home, tmp_md):
    import forge_refs
    a = forge_refs.register(label="x.md", abs_path=str(tmp_md), source_agent="milk")
    b = forge_refs.register(label="x.md", abs_path=str(tmp_md), source_agent="judy")
    assert a["id"] != b["id"]


def test_register_rejects_relative(fake_home):
    import forge_refs
    with pytest.raises(forge_refs.RefValidationError):
        forge_refs.register(label="x.md", abs_path="relative/path.md", source_agent="milk")


def test_register_rejects_missing(fake_home, tmp_path):
    import forge_refs
    with pytest.raises(forge_refs.RefValidationError):
        forge_refs.register(label="x.md", abs_path=str(tmp_path / "nope.md"), source_agent="milk")


def test_register_rejects_directory(fake_home, tmp_path):
    import forge_refs
    with pytest.raises(forge_refs.RefValidationError):
        forge_refs.register(label="x.md", abs_path=str(tmp_path), source_agent="milk")


def test_register_rejects_symlink(fake_home, tmp_path, tmp_md):
    import forge_refs
    link = tmp_path / "link.md"
    link.symlink_to(tmp_md)
    with pytest.raises(forge_refs.RefBlockedError):
        forge_refs.register(label="link.md", abs_path=str(link), source_agent="milk")


def test_register_rejects_oversize(fake_home, tmp_path):
    import forge_refs
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * (forge_refs.MAX_BYTES + 1))
    with pytest.raises(forge_refs.RefTooLargeError):
        forge_refs.register(label="big.md", abs_path=str(big), source_agent="milk")


def test_register_validation_empty_label_agent(fake_home, tmp_md):
    import forge_refs
    with pytest.raises(forge_refs.RefValidationError):
        forge_refs.register(label="", abs_path=str(tmp_md), source_agent="milk")
    with pytest.raises(forge_refs.RefValidationError):
        forge_refs.register(label="ok.md", abs_path=str(tmp_md), source_agent="")
    with pytest.raises(forge_refs.RefValidationError):
        forge_refs.register(label="bad/label.md", abs_path=str(tmp_md), source_agent="milk")


def test_list_filters(fake_home, tmp_path):
    import forge_refs
    a = tmp_path / "a.md"; a.write_text("a")
    b = tmp_path / "b.md"; b.write_text("b")
    c = tmp_path / "c.md"; c.write_text("c")
    r1 = forge_refs.register(label="a.md", abs_path=str(a), source_agent="milk", thread_id="th_1")
    r2 = forge_refs.register(label="b.md", abs_path=str(b), source_agent="judy", thread_id="th_1", squad_id="sq")
    r3 = forge_refs.register(label="c.md", abs_path=str(c), source_agent="milk", squad_id="sq")
    all_ids = {r["id"] for r in forge_refs.list_refs()}
    assert all_ids == {r1["id"], r2["id"], r3["id"]}
    milk = [r["id"] for r in forge_refs.list_refs(agent="milk")]
    assert set(milk) == {r1["id"], r3["id"]}
    th1 = [r["id"] for r in forge_refs.list_refs(thread="th_1")]
    assert set(th1) == {r1["id"], r2["id"]}
    sq = [r["id"] for r in forge_refs.list_refs(squad="sq")]
    assert set(sq) == {r2["id"], r3["id"]}


def test_read_content_markdown(fake_home, tmp_md):
    import forge_refs
    ref = forge_refs.register(label="note.md", abs_path=str(tmp_md), source_agent="milk")
    data, mime, ref2 = forge_refs.read_content(ref["id"])
    assert b"hello" in data
    assert mime.startswith("text/")
    assert ref2["id"] == ref["id"]


def test_read_content_missing_file(fake_home, tmp_md):
    import forge_refs
    ref = forge_refs.register(label="note.md", abs_path=str(tmp_md), source_agent="milk")
    tmp_md.unlink()
    with pytest.raises(forge_refs.RefMissingError):
        forge_refs.read_content(ref["id"])


def test_read_content_not_found(fake_home):
    import forge_refs
    with pytest.raises(forge_refs.RefNotFoundError):
        forge_refs.read_content("ref_deadbeef")


def test_read_content_blocked_mime(fake_home, tmp_path):
    import forge_refs
    bad = tmp_path / "evil.exe"
    bad.write_bytes(b"MZ\x90\x00")
    ref = forge_refs.register(
        label="evil.exe", abs_path=str(bad), source_agent="milk",
        content_type="application/x-msdownload",
    )
    with pytest.raises(forge_refs.RefBlockedError):
        forge_refs.read_content(ref["id"])


def test_write_content_requires_writable(fake_home, tmp_md):
    import forge_refs
    ref = forge_refs.register(label="note.md", abs_path=str(tmp_md), source_agent="milk")
    with pytest.raises(forge_refs.RefReadOnlyError):
        forge_refs.write_content(ref["id"], b"new")


def test_write_content_ok(fake_home, tmp_md):
    import forge_refs
    ref = forge_refs.register(
        label="note.md", abs_path=str(tmp_md), source_agent="milk", writable=True,
    )
    out = forge_refs.write_content(ref["id"], b"updated\n")
    assert out["size"] == len(b"updated\n")
    assert tmp_md.read_bytes() == b"updated\n"


def test_write_content_too_large(fake_home, tmp_md):
    import forge_refs
    ref = forge_refs.register(
        label="note.md", abs_path=str(tmp_md), source_agent="milk", writable=True,
    )
    with pytest.raises(forge_refs.RefTooLargeError):
        forge_refs.write_content(ref["id"], b"x" * (forge_refs.MAX_BYTES + 1))


def test_write_content_invalid_body_type(fake_home, tmp_md):
    import forge_refs
    ref = forge_refs.register(
        label="note.md", abs_path=str(tmp_md), source_agent="milk", writable=True,
    )
    with pytest.raises(forge_refs.RefValidationError):
        forge_refs.write_content(ref["id"], "not bytes")  # type: ignore[arg-type]


def test_unregister_then_404(fake_home, tmp_md):
    import forge_refs
    ref = forge_refs.register(label="note.md", abs_path=str(tmp_md), source_agent="milk")
    assert forge_refs.unregister(ref["id"]) is True
    assert forge_refs.get_ref(ref["id"]) is None
    with pytest.raises(forge_refs.RefNotFoundError):
        forge_refs.read_content(ref["id"])
    assert forge_refs.unregister(ref["id"]) is False


def test_re_register_after_unregister_new_id(fake_home, tmp_md):
    import forge_refs
    a = forge_refs.register(label="note.md", abs_path=str(tmp_md), source_agent="milk")
    forge_refs.unregister(a["id"])
    b = forge_refs.register(label="note.md", abs_path=str(tmp_md), source_agent="milk")
    assert a["id"] != b["id"]


def test_jsonl_replay_after_reset(fake_home, tmp_md):
    import forge_refs
    ref = forge_refs.register(label="note.md", abs_path=str(tmp_md), source_agent="milk")
    forge_refs._reset_for_tests()
    got = forge_refs.get_ref(ref["id"])
    assert got and got["id"] == ref["id"]


# ─── HTTP route tests ──────────────────────────────────────────────────


def _req(method, url, body=None, raw=None, headers=None):
    data = raw
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    return urllib.request.Request(url, data=data, headers=h, method=method)


def _call(method, url, body=None, raw=None, headers=None):
    try:
        with urllib.request.urlopen(_req(method, url, body, raw, headers), timeout=3) as r:
            txt = r.read().decode("utf-8", errors="replace")
            if r.status == 204:
                return r.status, None, dict(r.headers)
            try:
                return r.status, json.loads(txt), dict(r.headers)
            except Exception:
                return r.status, txt, dict(r.headers)
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(txt), dict(e.headers)
        except Exception:
            return e.code, txt, dict(e.headers)


def test_http_register_and_list(server, tmp_path):
    base = server
    note = tmp_path / "note.md"
    note.write_text("# from HTTP\n")
    status, body, _ = _call("POST", f"{base}/api/refs", {
        "label": "note.md", "abs_path": str(note), "source_agent": "milk",
    })
    assert status == 201, body
    rid = body["id"]
    status, body, _ = _call("GET", f"{base}/api/refs")
    assert status == 200
    assert any(r["id"] == rid for r in body["refs"])


def test_http_get_one(server, tmp_path):
    base = server
    note = tmp_path / "note.md"
    note.write_text("# x\n")
    _, body, _ = _call("POST", f"{base}/api/refs", {
        "label": "note.md", "abs_path": str(note), "source_agent": "milk",
    })
    rid = body["id"]
    status, body, _ = _call("GET", f"{base}/api/refs/{rid}")
    assert status == 200
    assert body["id"] == rid
    status, _, _ = _call("GET", f"{base}/api/refs/ref_deadbeef")
    assert status == 404


def test_http_content_round_trip(server, tmp_path):
    base = server
    note = tmp_path / "note.md"
    note.write_text("hello world\n")
    _, body, _ = _call("POST", f"{base}/api/refs", {
        "label": "note.md", "abs_path": str(note), "source_agent": "milk",
    })
    rid = body["id"]
    status, body, headers = _call("GET", f"{base}/api/refs/{rid}/content")
    assert status == 200
    assert "hello world" in (body if isinstance(body, str) else "")
    assert headers.get("X-Ref-Label") == "note.md"
    assert headers.get("X-Ref-Source-Agent") == "milk"


def test_http_put_content_403_when_not_writable(server, tmp_path):
    base = server
    note = tmp_path / "note.md"
    note.write_text("v1\n")
    _, body, _ = _call("POST", f"{base}/api/refs", {
        "label": "note.md", "abs_path": str(note), "source_agent": "milk",
    })
    rid = body["id"]
    status, _, _ = _call(
        "PUT", f"{base}/api/refs/{rid}/content",
        raw=b"v2", headers={"Content-Type": "text/plain"},
    )
    assert status == 403


def test_http_put_content_ok_when_writable(server, tmp_path):
    base = server
    note = tmp_path / "note.md"
    note.write_text("v1\n")
    _, body, _ = _call("POST", f"{base}/api/refs", {
        "label": "note.md", "abs_path": str(note),
        "source_agent": "milk", "writable": True,
    })
    rid = body["id"]
    status, body, _ = _call(
        "PUT", f"{base}/api/refs/{rid}/content",
        raw=b"v2", headers={"Content-Type": "text/plain"},
    )
    assert status == 200, body
    assert body["size"] == 2
    assert note.read_text() == "v2"


def test_http_delete_then_404(server, tmp_path):
    base = server
    note = tmp_path / "note.md"
    note.write_text("x")
    _, body, _ = _call("POST", f"{base}/api/refs", {
        "label": "note.md", "abs_path": str(note), "source_agent": "milk",
    })
    rid = body["id"]
    status, _, _ = _call("DELETE", f"{base}/api/refs/{rid}")
    assert status == 204
    status, _, _ = _call("GET", f"{base}/api/refs/{rid}")
    assert status == 404
    status, _, _ = _call("GET", f"{base}/api/refs/{rid}/content")
    assert status == 404
    status, _, _ = _call("DELETE", f"{base}/api/refs/{rid}")
    assert status == 404


def test_http_register_validation_errors(server, tmp_path):
    base = server
    # missing abs_path
    status, body, _ = _call("POST", f"{base}/api/refs", {
        "label": "x.md", "source_agent": "milk",
    })
    assert status == 400
    # relative path
    status, _, _ = _call("POST", f"{base}/api/refs", {
        "label": "x.md", "abs_path": "relative.md", "source_agent": "milk",
    })
    assert status == 400
    # missing file
    status, _, _ = _call("POST", f"{base}/api/refs", {
        "label": "x.md", "abs_path": str(tmp_path / "nope.md"), "source_agent": "milk",
    })
    assert status == 400


def test_http_register_blocked_symlink(server, tmp_path):
    base = server
    real = tmp_path / "real.md"
    real.write_text("ok")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    status, body, _ = _call("POST", f"{base}/api/refs", {
        "label": "link.md", "abs_path": str(link), "source_agent": "milk",
    })
    assert status == 403


def test_http_filter_by_agent(server, tmp_path):
    base = server
    a = tmp_path / "a.md"; a.write_text("a")
    b = tmp_path / "b.md"; b.write_text("b")
    _call("POST", f"{base}/api/refs",
          {"label": "a.md", "abs_path": str(a), "source_agent": "milk"})
    _call("POST", f"{base}/api/refs",
          {"label": "b.md", "abs_path": str(b), "source_agent": "judy"})
    status, body, _ = _call("GET", f"{base}/api/refs?agent=milk")
    assert status == 200
    labels = [r["label"] for r in body["refs"]]
    assert labels == ["a.md"]


def test_http_bad_json_400(server):
    base = server
    req = urllib.request.Request(
        f"{base}/api/refs",
        data=b"not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3):
            pytest.fail("expected 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400
