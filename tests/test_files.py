"""Tests for forge_files module + /api/files HTTP routes."""
# ruff: noqa: F811  (pytest fixture re-import is intentional)
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

# Reuse server fixture machinery via the existing tests/test_server.py.
# Use `pytest_plugins` so pytest registers the fixture without import gymnastics.
pytest_plugins = ["tests.test_server"]

# ─── unit tests against the module directly ──────────────────────────

def test_files_root_creates_dir(fake_home):
    import forge_files
    root = forge_files.files_root()
    assert root.exists() and root.is_dir()
    assert root == fake_home / ".openforge" / "files"


def test_list_files_empty(fake_home):
    import forge_files
    assert forge_files.list_files() == []


def test_create_then_list_then_read(fake_home):
    import forge_files
    meta = forge_files.create_file("hello.md", "# hi\n")
    assert meta["name"] == "hello.md"
    assert meta["size"] == len(b"# hi\n")
    listing = forge_files.list_files()
    assert [f["name"] for f in listing] == ["hello.md"]
    got = forge_files.read_file("hello.md")
    assert got["content"] == "# hi\n"
    assert got["size"] == meta["size"]


def test_create_default_content_empty(fake_home):
    import forge_files
    meta = forge_files.create_file("empty.md")
    assert meta["size"] == 0
    assert forge_files.read_file("empty.md")["content"] == ""


def test_create_duplicate_raises(fake_home):
    import forge_files
    forge_files.create_file("dup.md", "x")
    with pytest.raises(forge_files.AlreadyExistsError):
        forge_files.create_file("dup.md", "y")


def test_update_overwrites(fake_home):
    import forge_files
    forge_files.create_file("u.md", "v1")
    meta = forge_files.update_file("u.md", "v2-much-longer")
    assert meta["size"] == len(b"v2-much-longer")
    assert forge_files.read_file("u.md")["content"] == "v2-much-longer"


def test_update_missing_raises(fake_home):
    import forge_files
    with pytest.raises(forge_files.NotFoundError):
        forge_files.update_file("missing.md", "x")


def test_read_missing_raises(fake_home):
    import forge_files
    with pytest.raises(forge_files.NotFoundError):
        forge_files.read_file("nope.md")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "no-extension",
        "foo.txt",
        "../etc/passwd",
        "foo/bar.md",
        "with space.md",
        ".md",
        "x" * 300 + ".md.exe",
        None,
        123,
    ],
)
def test_validate_name_rejects(fake_home, bad):
    import forge_files
    with pytest.raises(forge_files.FileNameError):
        forge_files.read_file(bad)  # type: ignore[arg-type]


def test_non_string_content_rejected(fake_home):
    import forge_files
    with pytest.raises(forge_files.FileNameError):
        forge_files.create_file("x.md", 123)  # type: ignore[arg-type]
    forge_files.create_file("y.md", "")
    with pytest.raises(forge_files.FileNameError):
        forge_files.update_file("y.md", 456)  # type: ignore[arg-type]


def test_oversize_content_rejected(fake_home):
    import forge_files
    huge = "x" * (forge_files.MAX_CONTENT_BYTES + 1)
    with pytest.raises(forge_files.FileNameError):
        forge_files.create_file("big.md", huge)
    forge_files.create_file("big.md", "ok")
    with pytest.raises(forge_files.FileNameError):
        forge_files.update_file("big.md", huge)


def test_list_skips_non_md_and_subdirs(fake_home):
    import forge_files
    root = forge_files.files_root()
    (root / "a.md").write_text("a")
    (root / "skip.txt").write_text("nope")
    (root / "sub").mkdir()
    (root / "sub" / "b.md").write_text("ignored")
    names = [f["name"] for f in forge_files.list_files()]
    assert names == ["a.md"]


# ─── HTTP route tests ─────────────────────────────────────────────────

def _req(method: str, url: str, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return req


def _call(method: str, url: str, body=None):
    """Return (status, json_or_text)."""
    try:
        with urllib.request.urlopen(_req(method, url, body), timeout=3) as r:
            txt = r.read().decode("utf-8")
            return r.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, txt


def test_http_list_empty(server):
    base = server
    status, body = _call("GET", f"{base}/api/files")
    assert status == 200
    assert body["files"] == [] and body.get("root") == "files"


def test_http_full_crud_cycle(server):
    base = server
    # create
    status, body = _call("POST", f"{base}/api/files",
                         {"name": "notes.md", "content": "# hi"})
    assert status == 201, body
    assert body["name"] == "notes.md"
    assert body["size"] == len(b"# hi")
    # list
    status, body = _call("GET", f"{base}/api/files")
    assert status == 200
    assert [f["name"] for f in body["files"]] == ["notes.md"]
    # read
    status, body = _call("GET", f"{base}/api/files/notes.md")
    assert status == 200
    assert body["content"] == "# hi"
    # update
    status, body = _call("PUT", f"{base}/api/files/notes.md", {"content": "v2"})
    assert status == 200
    assert body["size"] == 2
    # re-read
    status, body = _call("GET", f"{base}/api/files/notes.md")
    assert body["content"] == "v2"


def test_http_create_default_empty_content(server):
    base = server
    status, body = _call("POST", f"{base}/api/files", {"name": "blank.md"})
    assert status == 201
    assert body["size"] == 0
    status, body = _call("GET", f"{base}/api/files/blank.md")
    assert body["content"] == ""


def test_http_duplicate_post_409(server):
    base = server
    _call("POST", f"{base}/api/files", {"name": "dup.md", "content": "a"})
    status, body = _call("POST", f"{base}/api/files", {"name": "dup.md", "content": "b"})
    assert status == 409
    assert "exists" in body["error"]


def test_http_get_missing_404(server):
    base = server
    status, body = _call("GET", f"{base}/api/files/missing.md")
    assert status == 404


def test_http_put_missing_404(server):
    base = server
    status, body = _call("PUT", f"{base}/api/files/missing.md", {"content": "x"})
    assert status == 404


def test_http_put_missing_content_400(server):
    base = server
    _call("POST", f"{base}/api/files", {"name": "x.md"})
    status, body = _call("PUT", f"{base}/api/files/x.md", {})
    assert status == 400


@pytest.mark.parametrize("bad", ["../etc/passwd", "foo.txt", "foo", "with space.md"])
def test_http_invalid_filename_in_post_400(server, bad):
    base = server
    status, body = _call("POST", f"{base}/api/files", {"name": bad})
    assert status == 400
    assert body["error"] == "invalid filename"


@pytest.mark.parametrize("bad", ["foo", "foo.txt", "..", "weird%file.md"])
def test_http_invalid_filename_in_get_400(server, bad):
    base = server
    # URL-encode minimally — leaving path-ish bits intact triggers route mismatch
    status, body = _call("GET", f"{base}/api/files/{bad}")
    assert status in (400, 404)  # path-traversal-y inputs may not even reach handler


def test_http_post_bad_json_400(server):
    base = server
    req = urllib.request.Request(
        f"{base}/api/files",
        data=b"not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3):
            pytest.fail("expected 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400
