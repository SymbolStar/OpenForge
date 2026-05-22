"""Tests for v0.7 multi-root file API + back-compat for v0.6 routes."""
# ruff: noqa: F811
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

pytest_plugins = ["tests.test_server"]


# ─── unit tests against the module directly ──────────────────────────


def _write_config(fake_home, roots):
    cfg_dir = fake_home / ".openclaw" / "openforge"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"fileRoots": roots}), encoding="utf-8"
    )


def test_default_roots_when_no_config(fake_home):
    import forge_files
    roots = forge_files.load_roots()
    assert len(roots) == 1
    assert roots[0].id == "files"
    assert roots[0].writable is True
    assert roots[0].path == fake_home / ".openclaw" / "openforge" / "files"


def test_custom_roots_load(fake_home, tmp_path):
    import forge_files
    docs = tmp_path / "docs"
    docs.mkdir()
    extra = tmp_path / "ro"
    extra.mkdir()
    _write_config(fake_home, [
        {"id": "files", "label": "Files",
         "path": str(fake_home / ".openclaw" / "openforge" / "files"),
         "writable": True},
        {"id": "docs", "label": "Docs", "path": str(docs), "writable": True},
        {"id": "ro", "label": "ReadOnly", "path": str(extra),
         "writable": False, "globs": ["*.md"]},
    ])
    roots = {r.id: r for r in forge_files.load_roots()}
    assert set(roots) == {"files", "docs", "ro"}
    assert roots["ro"].writable is False
    assert roots["ro"].globs == ("*.md",)


def test_repo_placeholder_resolves(fake_home, monkeypatch):
    import forge_files
    _write_config(fake_home, [
        {"id": "repo", "label": "Repo", "path": "<openforge_repo>",
         "writable": False, "globs": ["*.md"]},
    ])
    roots = forge_files.load_roots()
    assert any(r.id == "repo" for r in roots)


def test_missing_root_path_skipped(fake_home, tmp_path):
    import forge_files
    _write_config(fake_home, [
        {"id": "files", "label": "Files",
         "path": str(fake_home / ".openclaw" / "openforge" / "files"),
         "writable": True},
        {"id": "ghost", "label": "Ghost", "path": str(tmp_path / "nope")},
    ])
    ids = [r.id for r in forge_files.load_roots()]
    assert "ghost" not in ids
    assert "files" in ids


def test_invalid_config_falls_back(fake_home):
    import forge_files
    cfg_dir = fake_home / ".openclaw" / "openforge"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text("not json {{{", encoding="utf-8")
    roots = forge_files.load_roots()
    assert len(roots) == 1 and roots[0].id == "files"


def test_invalid_root_id_skipped(fake_home, tmp_path):
    import forge_files
    docs = tmp_path / "docs"
    docs.mkdir()
    _write_config(fake_home, [
        {"id": "bad/id", "label": "x", "path": str(docs)},
        {"id": "ok", "label": "OK", "path": str(docs)},
    ])
    ids = [r.id for r in forge_files.load_roots()]
    assert "bad/id" not in ids
    assert "ok" in ids


def test_duplicate_root_id_dedup(fake_home, tmp_path):
    import forge_files
    d = tmp_path / "d"
    d.mkdir()
    _write_config(fake_home, [
        {"id": "x", "label": "first", "path": str(d)},
        {"id": "x", "label": "second", "path": str(d)},
    ])
    roots = [r for r in forge_files.load_roots() if r.id == "x"]
    assert len(roots) == 1
    assert roots[0].label == "first"


def test_glob_filter_excludes_non_matches(fake_home, tmp_path):
    import forge_files
    d = tmp_path / "g"
    d.mkdir()
    (d / "yes.md").write_text("a")
    (d / "no.md").write_text("b")
    _write_config(fake_home, [
        {"id": "g", "label": "G", "path": str(d), "writable": True,
         "globs": ["yes.md"]},
    ])
    listing = forge_files.list_files("g")
    assert [f["name"] for f in listing] == ["yes.md"]
    # excluded files are also unreadable
    with pytest.raises(forge_files.NotFoundError):
        forge_files.read_file("no.md", "g")


def test_read_only_root_writes_rejected(fake_home, tmp_path):
    import forge_files
    d = tmp_path / "ro"
    d.mkdir()
    (d / "x.md").write_text("hi")
    _write_config(fake_home, [
        {"id": "files", "label": "F",
         "path": str(fake_home / ".openclaw" / "openforge" / "files"),
         "writable": True},
        {"id": "ro", "label": "RO", "path": str(d), "writable": False},
    ])
    with pytest.raises(forge_files.ReadOnlyError):
        forge_files.create_file("new.md", "x", "ro")
    with pytest.raises(forge_files.ReadOnlyError):
        forge_files.update_file("x.md", "y", "ro")
    # reads still work
    assert forge_files.read_file("x.md", "ro")["content"] == "hi"


def test_path_traversal_blocked(fake_home):
    import forge_files
    with pytest.raises(forge_files.FileNameError):
        forge_files.read_file("../../etc/passwd")
    with pytest.raises(forge_files.FileNameError):
        forge_files.read_file("a/b.md")
    with pytest.raises(forge_files.FileNameError):
        forge_files.create_file(".hidden.md", "x")


def test_name_with_dots_allowed(fake_home):
    import forge_files
    meta = forge_files.create_file("v0.7-notes.md", "ok")
    assert meta["name"] == "v0.7-notes.md"
    assert forge_files.read_file("v0.7-notes.md")["content"] == "ok"


def test_unknown_root_id(fake_home):
    import forge_files
    with pytest.raises(forge_files.NotFoundError):
        forge_files.list_files("nope")
    with pytest.raises(forge_files.NotFoundError):
        forge_files.read_file("x.md", "nope")
    with pytest.raises(forge_files.NotFoundError):
        forge_files.create_file("x.md", "y", "nope")
    with pytest.raises(forge_files.NotFoundError):
        forge_files.update_file("x.md", "y", "nope")


def test_list_file_roots_counts(fake_home, tmp_path):
    import forge_files
    d = tmp_path / "more"
    d.mkdir()
    (d / "a.md").write_text("a")
    (d / "b.md").write_text("b")
    (d / "c.txt").write_text("skip")
    _write_config(fake_home, [
        {"id": "files", "label": "Files",
         "path": str(fake_home / ".openclaw" / "openforge" / "files"),
         "writable": True},
        {"id": "more", "label": "More", "path": str(d), "writable": True},
    ])
    summary = {s["id"]: s for s in forge_files.list_file_roots()}
    assert summary["files"]["count"] == 0
    assert summary["more"]["count"] == 2
    assert summary["more"]["label"] == "More"


# ─── HTTP route tests ────────────────────────────────────────────────


def _req(method, url, body=None, headers=None):
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=data, headers=h, method=method)


def _call(method, url, body=None):
    try:
        resp = urllib.request.urlopen(_req(method, url, body), timeout=5)
        raw = resp.read().decode("utf-8")
        return resp.status, dict(resp.headers), json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, dict(e.headers), json.loads(raw)
        except Exception:
            return e.code, dict(e.headers), raw


def test_http_file_roots_endpoint(server, fake_home):
    status, _hdr, body = _call("GET", f"{server}/api/file-roots")
    assert status == 200
    assert "roots" in body
    ids = [r["id"] for r in body["roots"]]
    assert "files" in ids


def test_http_list_files_with_root_param(server, fake_home):
    _write_config(fake_home, [
        {"id": "files", "label": "Files",
         "path": str(fake_home / ".openclaw" / "openforge" / "files"),
         "writable": True},
    ])
    status, _hdr, body = _call("GET", f"{server}/api/files?root=files")
    assert status == 200
    assert body["root"] == "files"
    assert body["files"] == []


def test_http_list_files_unknown_root(server):
    status, _hdr, _body = _call("GET", f"{server}/api/files?root=ghost")
    assert status == 404


def test_http_per_root_crud(server, fake_home, tmp_path):
    # multi-root config: default + 'docs'
    docs = tmp_path / "docs-root"
    docs.mkdir()
    _write_config(fake_home, [
        {"id": "files", "label": "Files",
         "path": str(fake_home / ".openclaw" / "openforge" / "files"),
         "writable": True},
        {"id": "docs", "label": "Docs", "path": str(docs), "writable": True},
    ])
    # POST /api/files/docs
    status, _h, body = _call("POST", f"{server}/api/files/docs",
                             {"name": "hi.md", "content": "# hi"})
    assert status == 201
    assert body["name"] == "hi.md"
    # GET /api/files/docs/hi.md
    status, _h, body = _call("GET", f"{server}/api/files/docs/hi.md")
    assert status == 200
    assert body["content"] == "# hi"
    assert body["root"] == "docs"
    # PUT
    status, _h, body = _call("PUT", f"{server}/api/files/docs/hi.md",
                             {"content": "# updated"})
    assert status == 200
    # list
    status, _h, body = _call("GET", f"{server}/api/files?root=docs")
    assert [f["name"] for f in body["files"]] == ["hi.md"]


def test_http_unknown_root_for_crud(server, fake_home):
    status, _h, _ = _call("GET", f"{server}/api/files/ghost/x.md")
    assert status == 404
    status, _h, _ = _call("POST", f"{server}/api/files/ghost",
                          {"name": "x.md"})
    assert status == 404
    status, _h, _ = _call("PUT", f"{server}/api/files/ghost/x.md",
                          {"content": "y"})
    assert status == 404


def test_http_read_only_root_rejects_writes(server, fake_home, tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    (ro / "doc.md").write_text("hello")
    _write_config(fake_home, [
        {"id": "files", "label": "Files",
         "path": str(fake_home / ".openclaw" / "openforge" / "files"),
         "writable": True},
        {"id": "ro", "label": "RO", "path": str(ro), "writable": False},
    ])
    status, _h, _ = _call("POST", f"{server}/api/files/ro",
                          {"name": "x.md", "content": "y"})
    assert status == 403
    status, _h, _ = _call("PUT", f"{server}/api/files/ro/doc.md",
                          {"content": "y"})
    assert status == 403
    # but GET works
    status, _h, body = _call("GET", f"{server}/api/files/ro/doc.md")
    assert status == 200
    assert body["content"] == "hello"


def test_http_v06_routes_still_work_with_deprecation_header(server, fake_home):
    # POST /api/files (no root) -> first root, with Deprecation header
    status, hdr, body = _call("POST", f"{server}/api/files",
                              {"name": "compat.md", "content": "old"})
    assert status == 201
    assert "Deprecation" in hdr and "Sunset" in hdr
    assert "deprecated" in (hdr.get("Warning") or "").lower()
    # GET single-segment
    status, hdr, body = _call("GET", f"{server}/api/files/compat.md")
    assert status == 200
    assert "Deprecation" in hdr
    assert body["content"] == "old"
    # PUT single-segment
    status, hdr, body = _call("PUT", f"{server}/api/files/compat.md",
                              {"content": "new"})
    assert status == 200
    assert "Deprecation" in hdr


def test_http_invalid_filename_in_root_path(server, fake_home):
    status, _h, _ = _call("GET", f"{server}/api/files/files/not%20a%20file")
    assert status == 400


def test_http_path_traversal_blocked(server, fake_home):
    # POST with traversal name -> 400 (filename invalid)
    status, _h, _ = _call("POST", f"{server}/api/files",
                          {"name": "../etc/passwd.md"})
    assert status == 400


def test_http_dotfile_blocked(server, fake_home):
    status, _h, _ = _call("POST", f"{server}/api/files",
                          {"name": ".hidden.md", "content": "x"})
    assert status == 400
