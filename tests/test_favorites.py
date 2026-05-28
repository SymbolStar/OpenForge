"""Tests for forge_favorites + /api/favorites HTTP routes (PRD v1.1)."""
# ruff: noqa: F811
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import pytest

pytest_plugins = ["tests.test_server"]


@pytest.fixture(autouse=True)
def _reset_module(fake_home):
    import forge_favorites
    forge_favorites._reset_for_tests()
    yield
    forge_favorites._reset_for_tests()


@pytest.fixture
def tmp_md(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("# Hello\nbody line\n", encoding="utf-8")
    return p


# ─── unit ───────────────────────────────────────────────────────────


def test_set_then_get(fake_home, tmp_md):
    import forge_favorites
    rec = forge_favorites.set_favorite(str(tmp_md), source_agent="milk", thread_id="th_x")
    assert rec["abs_path"] == str(tmp_md)
    assert rec["first_seen_agent"] == "milk"
    assert rec["first_seen_thread_id"] == "th_x"
    assert forge_favorites.is_favorite(str(tmp_md))


def test_set_idempotent_pk(fake_home, tmp_md):
    """AC-10: repeated PATCH true on same abs_path yields exactly one row."""
    import forge_favorites
    for i in range(5):
        forge_favorites.set_favorite(str(tmp_md), source_agent=f"agent{i}", thread_id=f"th_{i}")
    rows = forge_favorites.list_favorites()
    assert len(rows) == 1
    # first_seen_* preserved from very first set
    assert rows[0]["first_seen_agent"] == "agent0"
    assert rows[0]["first_seen_thread_id"] == "th_0"


def test_unset_removes(fake_home, tmp_md):
    import forge_favorites
    forge_favorites.set_favorite(str(tmp_md), source_agent="x")
    assert forge_favorites.unset_favorite(str(tmp_md)) is True
    assert not forge_favorites.is_favorite(str(tmp_md))
    assert forge_favorites.unset_favorite(str(tmp_md)) is False


def test_validation_rejects_relative(fake_home):
    import forge_favorites
    with pytest.raises(forge_favorites.FavoriteValidationError):
        forge_favorites.set_favorite("relative/path.md")
    with pytest.raises(forge_favorites.FavoriteValidationError):
        forge_favorites.set_favorite("")


def test_persists_across_reload(fake_home, tmp_md):
    import forge_favorites
    forge_favorites.set_favorite(str(tmp_md), source_agent="milk")
    forge_favorites._reset_for_tests()
    rows = forge_favorites.list_favorites()
    assert len(rows) == 1
    assert rows[0]["abs_path"] == str(tmp_md)


def test_list_sorted_by_time_desc(fake_home, tmp_path):
    import forge_favorites
    a = tmp_path / "a.md"; a.write_text("# A\n")
    b = tmp_path / "b.md"; b.write_text("# B\n")
    forge_favorites.set_favorite(str(a))
    time.sleep(0.01)
    forge_favorites.set_favorite(str(b))
    rows = forge_favorites.list_favorites()
    assert [r["abs_path"] for r in rows] == [str(b), str(a)]


def test_list_with_status_present(fake_home, tmp_md):
    import forge_favorites
    forge_favorites.set_favorite(str(tmp_md), source_agent="milk")
    out = forge_favorites.list_with_status()
    assert len(out) == 1
    row = out[0]
    assert row["missing_state"] == "present"
    assert row["preview"] == "Hello"
    assert row["label"] == "note.md"


def test_list_with_status_missing(fake_home, tmp_path):
    """AC-7: deleted file → missing_state='missing'."""
    import forge_favorites
    p = tmp_path / "gone.md"; p.write_text("# X\n")
    forge_favorites.set_favorite(str(p))
    p.unlink()
    out = forge_favorites.list_with_status()
    assert out[0]["missing_state"] == "missing"


def test_preview_fallback_to_first_line(fake_home, tmp_path):
    import forge_favorites
    p = tmp_path / "no-heading.md"
    p.write_text("\n\nfirst real line of text\nsecond line\n", encoding="utf-8")
    forge_favorites.set_favorite(str(p))
    out = forge_favorites.list_with_status()
    assert out[0]["preview"] == "first real line of text"


# ─── HTTP ──────────────────────────────────────────────────────────


def _patch_favorites(server, body):
    url = f"{server.rstrip('/')}/api/favorites"
    req = urllib.request.Request(
        url, method="PATCH",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8"),
    )
    return urllib.request.urlopen(req, timeout=5)


def _get_favorites(server):
    url = f"{server.rstrip('/')}/api/favorites"
    return urllib.request.urlopen(url, timeout=5)


def test_http_patch_then_get(server, tmp_md):
    r = _patch_favorites(server, {
        "abs_path": str(tmp_md),
        "favorited": True,
        "source_agent": "milk",
        "thread_id": "th_a",
    })
    assert r.status == 200
    body = json.loads(r.read())
    assert body["favorited"] is True

    r = _get_favorites(server)
    payload = json.loads(r.read())
    assert payload["count"] == 1
    assert payload["favorites"][0]["abs_path"] == str(tmp_md)
    assert payload["favorites"][0]["source_agent"] == "milk"
    assert payload["favorites"][0]["source_thread_id"] == "th_a"
    assert payload["favorites"][0]["missing_state"] == "present"


def test_http_patch_unfavorite(server, tmp_md):
    _patch_favorites(server, {"abs_path": str(tmp_md), "favorited": True})
    _patch_favorites(server, {"abs_path": str(tmp_md), "favorited": False})
    r = _get_favorites(server)
    payload = json.loads(r.read())
    assert payload["count"] == 0


def test_http_patch_rejects_relative(server):
    try:
        _patch_favorites(server, {"abs_path": "relative.md", "favorited": True})
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_http_patch_rejects_missing_favorited(server, tmp_md):
    try:
        _patch_favorites(server, {"abs_path": str(tmp_md)})
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400
