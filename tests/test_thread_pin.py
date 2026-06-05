"""Tests for v0.10 thread-pin feature (forge_store.pin_thread_ref / unpin /
list_thread_pinned_refs and forge_refs.ref_exists)."""
from __future__ import annotations

import pytest


def _mk_thread(store):
    store.ensure_default_squads()
    store.create_squad({"id": "s1", "name": "s1", "members": ["m"], "chair": "m"})
    t = store.create_thread("s1", "scott", "open")
    return t["thread_id"]


def test_pin_unpin_round_trip(store):
    tid = _mk_thread(store)
    store.pin_thread_ref(tid, "ref_abc", actor="scott")
    pins = store.list_thread_pinned_refs(tid)
    assert len(pins) == 1
    assert pins[0]["ref_id"] == "ref_abc"
    assert pins[0]["pinned_by"] == "scott"

    store.unpin_thread_ref(tid, "ref_abc", actor="dora", label="PLAN.md")
    assert store.list_thread_pinned_refs(tid) == []

    # unpin emitted a system post
    t = store.project_thread(tid)
    sys_posts = [p for p in t["posts"] if p["speaker"] == "__system__"]
    assert len(sys_posts) == 1
    assert "PLAN.md" in sys_posts[0]["content"]
    assert "dora" in sys_posts[0]["content"]
    assert "取消" in sys_posts[0]["content"]


def test_pin_does_not_emit_system_post(store):
    tid = _mk_thread(store)
    store.pin_thread_ref(tid, "ref_a", actor="scott")
    t = store.project_thread(tid)
    assert not [p for p in t["posts"] if p["speaker"] == "__system__"]


def test_pin_duplicate_rejected(store):
    tid = _mk_thread(store)
    store.pin_thread_ref(tid, "ref_a", actor="scott")
    with pytest.raises(store.PinAlreadyExistsError):
        store.pin_thread_ref(tid, "ref_a", actor="scott")


def test_pin_cap_reached(store):
    tid = _mk_thread(store)
    for i in range(store.PIN_CAP):
        store.pin_thread_ref(tid, f"ref_{i}", actor="scott")
    with pytest.raises(store.PinCapReached):
        store.pin_thread_ref(tid, "ref_overflow", actor="scott")
    pins = store.list_thread_pinned_refs(tid)
    assert len(pins) == store.PIN_CAP


def test_pin_order_oldest_first(store):
    tid = _mk_thread(store)
    store.pin_thread_ref(tid, "ref_1", actor="scott")
    store.pin_thread_ref(tid, "ref_2", actor="dora")
    store.pin_thread_ref(tid, "ref_3", actor="alice")
    ids = [p["ref_id"] for p in store.list_thread_pinned_refs(tid)]
    assert ids == ["ref_1", "ref_2", "ref_3"]


def test_unpin_unknown_raises(store):
    tid = _mk_thread(store)
    with pytest.raises(store.PinNotFoundError):
        store.unpin_thread_ref(tid, "ref_nope", actor="scott")


def test_unpin_silent_skips_system_post(store):
    tid = _mk_thread(store)
    store.pin_thread_ref(tid, "ref_a", actor="scott")
    store.unpin_thread_ref(tid, "ref_a", actor="scott", emit_system_post=False)
    t = store.project_thread(tid)
    assert not [p for p in t["posts"] if p["speaker"] == "__system__"]


def test_ref_exists_basic(fake_home, tmp_path, monkeypatch):
    import forge_refs

    # registered + file present
    f = tmp_path / "hello.md"
    f.write_text("hi")
    ref = forge_refs.register(
        label="hello.md",
        abs_path=str(f),
        source_agent="bugfix",
    )
    assert forge_refs.ref_exists(ref["id"]) is True

    # file deleted on disk
    f.unlink()
    assert forge_refs.ref_exists(ref["id"]) is False

    # unknown ref id
    assert forge_refs.ref_exists("ref_does_not_exist") is False
