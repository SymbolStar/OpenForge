"""Unit + HTTP tests for the paste-image upload pipeline (forge_uploads)."""
# ruff: noqa: F811
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

import pytest

from tests.conftest import server  # noqa: F401, E402


# ─── tiny valid image fixtures ───────────────────────────────────────

# 1x1 PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# Minimal valid JPEG (SOI + APP0 JFIF header + EOI). ffmpeg/most viewers
# would still complain, but our magic-byte check only inspects the first
# 3 bytes \xff\xd8\xff.
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

# 1x1 GIF
GIF_BYTES = base64.b64decode("R0lGODlhAQABAIABAP///wAAACwAAAAAAQABAAACAkQBADs=")

# Minimal WEBP (RIFF...WEBP header, just enough to pass magic check)
WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00\x30\x01\x00\x9d\x01\x2a\x01\x00\x01\x00\x02\x00\x34\x25\xa4\x00\x03\x70\x00\xfe\xfb\x94\x00\x00"


def _post(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
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


def _get_raw(url: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


# ─── module-level unit tests (no HTTP) ───────────────────────────────

def test_unit_save_and_retrieve_png(fake_home):
    import forge_uploads
    meta = forge_uploads.save_upload(PNG_BYTES, "image/png")
    assert meta["content_type"] == "image/png"
    assert meta["size"] == len(PNG_BYTES)
    assert meta["url"].startswith("/api/uploads/")
    assert meta["filename"].endswith(".png")
    # filename is sha256(content) + .png
    assert len(meta["sha256"]) == 64

    resolved = forge_uploads.get_upload_path(meta["filename"])
    assert resolved is not None
    path, mime = resolved
    assert path.read_bytes() == PNG_BYTES
    assert mime == "image/png"


def test_unit_dedup_same_bytes(fake_home):
    import forge_uploads
    m1 = forge_uploads.save_upload(PNG_BYTES, "image/png")
    m2 = forge_uploads.save_upload(PNG_BYTES, "image/png")
    assert m1["filename"] == m2["filename"]


@pytest.mark.parametrize("data,mime,ok", [
    (PNG_BYTES, "image/png", True),
    (JPEG_BYTES, "image/jpeg", True),
    (GIF_BYTES, "image/gif", True),
    (WEBP_BYTES, "image/webp", True),
])
def test_unit_accepts_all_allowed_mimes(fake_home, data, mime, ok):
    import forge_uploads
    meta = forge_uploads.save_upload(data, mime)
    assert meta["content_type"].split(";")[0] in {mime, "image/jpeg"}


def test_unit_rejects_mismatched_magic(fake_home):
    import forge_uploads
    # Claim PNG, send JPEG bytes
    with pytest.raises(forge_uploads.UploadError) as ei:
        forge_uploads.save_upload(JPEG_BYTES, "image/png")
    assert "do not match" in str(ei.value)


def test_unit_rejects_unsupported_mime(fake_home):
    import forge_uploads
    with pytest.raises(forge_uploads.UploadError) as ei:
        forge_uploads.save_upload(b"<svg/>", "image/svg+xml")
    assert "unsupported" in str(ei.value)


def test_unit_rejects_empty(fake_home):
    import forge_uploads
    with pytest.raises(forge_uploads.UploadError):
        forge_uploads.save_upload(b"", "image/png")


def test_unit_rejects_oversize(fake_home, monkeypatch):
    import forge_uploads
    monkeypatch.setattr(forge_uploads, "MAX_BYTES", 100)
    with pytest.raises(forge_uploads.UploadError) as ei:
        forge_uploads.save_upload(b"x" * 200, "image/png")
    assert "too large" in str(ei.value)


@pytest.mark.parametrize("bad", [
    "..%2F..%2Fetc%2Fpasswd",
    "../etc/passwd",
    "foo.exe",
    "noext",
    "ZZZ.png",                # not hex
    "abcd.png",               # wrong length
    "/leading/slash.png",
    "",
])
def test_unit_get_rejects_path_traversal_and_invalid_filenames(fake_home, bad):
    import forge_uploads
    assert forge_uploads.get_upload_path(bad) is None


def test_unit_get_rejects_nonexistent(fake_home):
    import forge_uploads
    assert forge_uploads.get_upload_path("a" * 64 + ".png") is None


# ─── HTTP-level tests (real server) ──────────────────────────────────

def test_http_upload_png_roundtrip(server):
    code, body = _post(f"{server}/api/uploads", {
        "content_base64": base64.b64encode(PNG_BYTES).decode("ascii"),
        "content_type": "image/png",
    })
    assert code == 201, body
    assert body["url"].startswith("/api/uploads/")
    assert body["content_type"] == "image/png"

    code2, raw, ctype = _get_raw(f"{server}{body['url']}")
    assert code2 == 200
    assert raw == PNG_BYTES
    assert ctype.startswith("image/png")


def test_http_upload_dedup_returns_same_url(server):
    payload = {
        "content_base64": base64.b64encode(PNG_BYTES).decode("ascii"),
        "content_type": "image/png",
    }
    c1, b1 = _post(f"{server}/api/uploads", payload)
    c2, b2 = _post(f"{server}/api/uploads", payload)
    assert c1 == 201 and c2 == 201
    assert b1["url"] == b2["url"]
    assert b1["filename"] == b2["filename"]


def test_http_upload_rejects_bad_base64(server):
    code, body = _post(f"{server}/api/uploads", {
        "content_base64": "not!!!base64@@@",
        "content_type": "image/png",
    })
    assert code == 400
    assert "base64" in body["error"]


def test_http_upload_rejects_missing_content(server):
    code, body = _post(f"{server}/api/uploads", {"content_type": "image/png"})
    assert code == 400


def test_http_upload_rejects_mime_mismatch(server):
    code, body = _post(f"{server}/api/uploads", {
        "content_base64": base64.b64encode(JPEG_BYTES).decode("ascii"),
        "content_type": "image/png",
    })
    assert code == 400
    assert "do not match" in body["error"]


def test_http_upload_rejects_unsupported_mime(server):
    code, body = _post(f"{server}/api/uploads", {
        "content_base64": base64.b64encode(b"<svg/>").decode("ascii"),
        "content_type": "image/svg+xml",
    })
    assert code == 400


def test_http_get_unknown_upload_returns_404(server):
    code, _, _ = _get_raw(f"{server}/api/uploads/{'a' * 64}.png")
    assert code == 404


def test_http_get_rejects_path_traversal(server):
    # urllib will normalize, but the route regex itself disallows / and ..
    code, body, _ = _get_raw(f"{server}/api/uploads/..%2Fpasswd")
    assert code in (400, 404)


# ─── content embedding test (renderBody compat) ──────────────────────

def test_http_uploaded_url_is_referenced_in_post(server):
    """Round-trip: create squad/thread, upload image, post with ![](url),
    verify the post contains the URL verbatim so renderBody can find it."""
    code, _ = _post(f"{server}/api/squads", {
        "id": "img-test", "name": "img-test",
        "members": ["scott"], "chair": "scott",
    })
    assert code == 201

    code, thread = _post(f"{server}/api/squads/img-test/threads", {
        "content": "paste image test",
        "created_by": "scott",
    })
    assert code == 201
    tid = thread["thread_id"]

    code, up = _post(f"{server}/api/uploads", {
        "content_base64": base64.b64encode(PNG_BYTES).decode("ascii"),
        "content_type": "image/png",
    })
    assert code == 201
    url = up["url"]

    body_text = f"see screenshot:\n\n![paste]({url})"
    code, _ = _post(f"{server}/api/threads/{tid}/posts", {
        "content": body_text,
        "speaker": "scott",
    })
    assert code in (200, 201)

    # GET thread and confirm post contains the URL
    with urllib.request.urlopen(f"{server}/api/threads/{tid}", timeout=2) as r:
        detail = json.loads(r.read().decode("utf-8"))
    posts = detail["posts"]
    found = any(url in (p.get("content") or "") for p in posts)
    assert found, f"uploaded image URL {url!r} not found in any post"
