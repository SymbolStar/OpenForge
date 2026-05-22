"""Tests for forge_context: STATUS read/write, 3-source bundle, cache."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def ctx(fake_home, monkeypatch):
    import importlib
    import sys
    # Force fresh forge_context with the patched HOME.
    if "forge_context" in sys.modules:
        del sys.modules["forge_context"]
    import forge_context  # noqa: WPS433
    return forge_context


def _write_config(ctx, agents: dict) -> None:
    """Write ~/.openclaw/openforge/config.json with an agents map."""
    p = ctx.forge_dir() / "config.json"
    p.write_text(json.dumps({"agents": agents}), encoding="utf-8")


# ─── STATUS read/write ───────────────────────────────────────────────


def test_write_status_creates_workspace_and_file(ctx):
    info = ctx.write_status("sherry", "# Sherry STATUS\n\n## 当前焦点\n做日报\n")
    assert info["agent"] == "sherry"
    p = Path(info["path"])
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "> 最后更新：" in text
    assert "## 当前焦点" in text


def test_write_status_replaces_prior_stamp(ctx):
    ctx.write_status("sherry", "# Sherry STATUS\n\n> 最后更新：2020-01-01\n\n## 当前焦点\n旧\n")
    info = ctx.write_status("sherry", "# Sherry STATUS\n\n> 最后更新：1999-01-01\n\n## 当前焦点\n新\n")
    body = Path(info["path"]).read_text(encoding="utf-8")
    # only one stamp line, and it is recent (year starts with 20XX, not 1999)
    stamps = [ln for ln in body.splitlines() if ln.startswith("> 最后更新：")]
    assert len(stamps) == 1
    assert "1999-01-01" not in stamps[0]


def test_write_status_bad_agent(ctx):
    with pytest.raises(ctx.StatusError):
        ctx.write_status("../escape", "x")
    with pytest.raises(ctx.StatusError):
        ctx.write_status("", "x")
    with pytest.raises(ctx.StatusError):
        ctx.write_status("sherry", 123)  # type: ignore[arg-type]


def test_write_status_too_large(ctx):
    big = "x" * (65 * 1024)
    with pytest.raises(ctx.StatusError):
        ctx.write_status("sherry", big)


def test_read_status_missing_returns_none(ctx):
    assert ctx.read_status("ghost") is None


def test_read_status_roundtrip(ctx):
    ctx.write_status("milk", "# Milk\n\n## 当前焦点\n喝奶\n")
    cur = ctx.read_status("milk")
    assert cur is not None
    assert "喝奶" in cur["content"]
    assert cur["agent"] == "milk"


def test_patch_status_section_updates(ctx):
    ctx.write_status("sherry", "# Sherry\n\n## 当前焦点\n旧任务\n\n## 进行中任务\n- a\n")
    ctx.patch_status_section("sherry", "当前焦点", "新任务 ✅")
    cur = ctx.read_status("sherry")
    assert "新任务" in cur["content"]
    assert "## 进行中任务" in cur["content"]  # other section preserved
    assert "旧任务" not in cur["content"]


def test_patch_status_section_unknown(ctx):
    ctx.write_status("sherry", "# Sherry\n\n## 当前焦点\nx\n")
    with pytest.raises(ctx.StatusError):
        ctx.patch_status_section("sherry", "不存在的章节", "y")


def test_patch_status_section_no_status_yet(ctx):
    with pytest.raises(ctx.StatusError):
        ctx.patch_status_section("sherry", "当前焦点", "y")


# ─── config loading ──────────────────────────────────────────────────


def test_load_agent_config_defaults(ctx):
    cfg = ctx.load_agent_config("sherry")
    assert cfg["enabled"] is True
    assert cfg["main_session_turns"] == 20
    assert cfg["main_session_key"] == "agent:sherry:main"
    assert "status" in cfg["include"]


def test_load_agent_config_override(ctx):
    _write_config(ctx, {
        "sherry": {
            "mainSessionKey": "agent:sherry:main-overridden",
            "contextBundle": {
                "enabled": True,
                "main_session_turns": 5,
                "memory_top_k": 2,
                "cache_ttl_seconds": 30,
            },
        }
    })
    cfg = ctx.load_agent_config("sherry")
    assert cfg["main_session_turns"] == 5
    assert cfg["main_session_key"] == "agent:sherry:main-overridden"
    assert cfg["memory_top_k"] == 2


def test_load_agent_config_corrupt_file(ctx):
    p = ctx.forge_dir() / "config.json"
    p.write_text("{not json", encoding="utf-8")
    cfg = ctx.load_agent_config("sherry")
    # Falls back to defaults instead of raising.
    assert cfg["main_session_turns"] == 20


# ─── main_session source ─────────────────────────────────────────────


def _seed_main_session(home: Path, agent: str, turns: list[tuple[str, str]]) -> Path:
    """Create sessions.json + .jsonl with given (role, text) turns."""
    sess_dir = home / ".openclaw" / "agents" / agent / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    sid = "abcd1234"
    jsonl = sess_dir / f"{sid}.jsonl"
    with jsonl.open("w", encoding="utf-8") as fh:
        for i, (role, text) in enumerate(turns):
            fh.write(json.dumps({
                "type": "message",
                "message": {
                    "role": role,
                    "content": [{"type": "text", "text": text}],
                },
                "timestamp": 1_700_000_000 + i,
            }) + "\n")
    idx = sess_dir / "sessions.json"
    idx.write_text(json.dumps({
        f"agent:{agent}:main": {
            "sessionId": sid,
            "sessionFile": str(jsonl),
            "updatedAt": 1_700_000_999,
        }
    }), encoding="utf-8")
    return jsonl


def test_collect_main_session_extracts_turns(ctx, fake_home):
    _seed_main_session(fake_home, "sherry", [
        ("user", "做下日报"),
        ("assistant", "好，正在做"),
        ("user", "进展？"),
        ("assistant", "16:35 已入箱"),
    ])
    cfg = ctx.load_agent_config("sherry")
    cfg["main_session_turns"] = 10
    out = ctx.collect_main_session("sherry", cfg)
    assert out
    texts = [t["text"] for t in out["turns"]]
    assert "16:35 已入箱" in texts
    assert len(out["turns"]) == 4


def test_collect_main_session_truncates_to_max_turns(ctx, fake_home):
    _seed_main_session(fake_home, "sherry", [
        ("user", f"q{i}") for i in range(50)
    ])
    cfg = ctx.load_agent_config("sherry")
    cfg["main_session_turns"] = 3
    out = ctx.collect_main_session("sherry", cfg)
    assert len(out["turns"]) == 3
    # We keep tail
    assert out["turns"][-1]["text"] == "q49"


def test_collect_main_session_byte_budget(ctx, fake_home):
    _seed_main_session(fake_home, "sherry", [
        ("user", "x" * 1000),
        ("assistant", "y" * 1000),
        ("user", "tail"),
    ])
    cfg = ctx.load_agent_config("sherry")
    cfg["main_session_turns"] = 10
    cfg["main_session_max_bytes"] = 50
    out = ctx.collect_main_session("sherry", cfg)
    assert out["truncated"] is True
    assert out["turns"][-1]["text"] == "tail"


def test_collect_main_session_missing(ctx):
    cfg = ctx.load_agent_config("sherry")
    out = ctx.collect_main_session("sherry", cfg)
    assert out == {}


def test_collect_main_session_corrupt_jsonl(ctx, fake_home):
    sess_dir = fake_home / ".openclaw" / "agents" / "sherry" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    sid = "deadbeef"
    (sess_dir / f"{sid}.jsonl").write_text("not json\n{also bad\n", encoding="utf-8")
    (sess_dir / "sessions.json").write_text(json.dumps({
        "agent:sherry:main": {"sessionId": sid, "sessionFile": str(sess_dir / f"{sid}.jsonl")}
    }), encoding="utf-8")
    cfg = ctx.load_agent_config("sherry")
    assert ctx.collect_main_session("sherry", cfg) == {}


# ─── memory source ───────────────────────────────────────────────────


def _install_fake_memory_search(tmp_path: Path, payload: str | None, exit_code: int = 0) -> Path:
    """Write a fake openclaw bin that returns `payload` on `memory search`."""
    bin_path = tmp_path / "fake_openclaw_mem.sh"
    if payload is None:
        body = f"""#!/bin/bash
exit {exit_code}
"""
    else:
        # `payload` must be JSON-safe single-line text
        body = f"""#!/bin/bash
if [ "$1" = "memory" ] && [ "$2" = "search" ]; then
  cat <<'JSON'
{payload}
JSON
  exit {exit_code}
fi
exit 1
"""
    bin_path.write_text(body, encoding="utf-8")
    bin_path.chmod(0o755)
    return bin_path


def test_collect_memory_happy(ctx, tmp_path, monkeypatch):
    fake = _install_fake_memory_search(tmp_path, json.dumps({
        "hits": [
            {"path": "memory/2026-05-22.md", "snippet": "日报已发", "score": 0.9},
            {"path": "memory/2026-05-21.md", "snippet": "scott 喜欢简短", "score": 0.7},
        ]
    }))
    monkeypatch.setenv("OPENFORGE_OPENCLAW_BIN", str(fake))
    cfg = ctx.load_agent_config("sherry")
    out = ctx.collect_memory("sherry", cfg, query_hint="日报")
    assert out and len(out["hits"]) == 2
    assert out["hits"][0]["path"].endswith(".md")


def test_collect_memory_no_query(ctx):
    cfg = ctx.load_agent_config("sherry")
    assert ctx.collect_memory("sherry", cfg, query_hint=None) == {}
    assert ctx.collect_memory("sherry", cfg, query_hint="   ") == {}


def test_collect_memory_search_fails_gracefully(ctx, tmp_path, monkeypatch):
    fake = _install_fake_memory_search(tmp_path, None, exit_code=2)
    monkeypatch.setenv("OPENFORGE_OPENCLAW_BIN", str(fake))
    cfg = ctx.load_agent_config("sherry")
    assert ctx.collect_memory("sherry", cfg, query_hint="anything") == {}


def test_collect_memory_invalid_json(ctx, tmp_path, monkeypatch):
    fake = _install_fake_memory_search(tmp_path, "not json at all")
    monkeypatch.setenv("OPENFORGE_OPENCLAW_BIN", str(fake))
    cfg = ctx.load_agent_config("sherry")
    assert ctx.collect_memory("sherry", cfg, query_hint="x") == {}


def test_collect_memory_byte_budget_trims(ctx, tmp_path, monkeypatch):
    payload = json.dumps({
        "hits": [{"path": f"m{i}.md", "snippet": "x" * 200, "score": 0.5} for i in range(5)]
    })
    fake = _install_fake_memory_search(tmp_path, payload)
    monkeypatch.setenv("OPENFORGE_OPENCLAW_BIN", str(fake))
    cfg = ctx.load_agent_config("sherry")
    cfg["memory_max_bytes"] = 350
    out = ctx.collect_memory("sherry", cfg, query_hint="q")
    assert out["truncated"] is True
    assert 1 <= len(out["hits"]) <= 3


def test_collect_memory_missing_binary(ctx, monkeypatch):
    monkeypatch.setenv("OPENFORGE_OPENCLAW_BIN", "/no/such/binary/exists/here")
    cfg = ctx.load_agent_config("sherry")
    # Falls back to PATH `openclaw`; on CI/test this might or might not exist,
    # but the search command should fail gracefully either way.
    out = ctx.collect_memory("sherry", cfg, query_hint="x")
    assert out == {} or "hits" in out  # never raises


# ─── status source ────────────────────────────────────────────────────


def test_collect_status_truncation(ctx):
    long = "# x\n\n" + ("段落\n" * 5000)
    ctx.write_status("sherry", long)
    cfg = ctx.load_agent_config("sherry")
    cfg["status_max_bytes"] = 200
    out = ctx.collect_status("sherry", cfg)
    assert out["truncated"] is True
    assert len(out["content"].encode("utf-8")) <= 250  # tolerance for ellipsis tail


def test_collect_status_missing(ctx):
    cfg = ctx.load_agent_config("sherry")
    assert ctx.collect_status("sherry", cfg) == {}


# ─── build_context_bundle + cache ────────────────────────────────────


def test_build_bundle_three_sources(ctx, fake_home, tmp_path, monkeypatch):
    # Seed all three sources
    ctx.write_status("sherry", "# Sherry\n\n## 当前焦点\n日报 16:35 已入箱\n")
    _seed_main_session(fake_home, "sherry", [
        ("user", "进展？"),
        ("assistant", "16:35 已入箱"),
    ])
    fake = _install_fake_memory_search(tmp_path, json.dumps({
        "hits": [{"path": "memory/2026-05-22.md", "snippet": "scott 喜欢简短", "score": 0.9}]
    }))
    monkeypatch.setenv("OPENFORGE_OPENCLAW_BIN", str(fake))

    b = ctx.build_context_bundle("sherry", query_hint="日报")
    assert b.cache_hit is False
    assert "status" in b.sources
    assert "main_session" in b.sources
    assert "memory" in b.sources
    rendered = b.render()
    assert "16:35 已入箱" in rendered
    assert "STATUS" in rendered
    assert "scott 喜欢简短" in rendered


def test_build_bundle_all_sources_missing_returns_empty(ctx):
    # No status, no main, no query → empty bundle, no exception.
    b = ctx.build_context_bundle("ghost")
    assert b.sources == {}
    assert b.render() == ""


def test_build_bundle_cache_hit(ctx, fake_home):
    ctx.write_status("sherry", "# S\n\n## 当前焦点\nx\n")
    first = ctx.build_context_bundle("sherry")
    assert first.cache_hit is False
    second = ctx.build_context_bundle("sherry")
    assert second.cache_hit is True
    assert second.sources == first.sources


def test_build_bundle_cache_invalidated_by_status_write(ctx):
    ctx.write_status("sherry", "# S\n\n## 当前焦点\n旧\n")
    ctx.build_context_bundle("sherry")  # warm cache
    ctx.write_status("sherry", "# S\n\n## 当前焦点\n新\n")
    b = ctx.build_context_bundle("sherry")
    assert b.cache_hit is False
    assert "新" in b.render()


def test_build_bundle_force_refresh(ctx):
    ctx.write_status("sherry", "# S\n\n## 当前焦点\nx\n")
    ctx.build_context_bundle("sherry")
    b = ctx.build_context_bundle("sherry", force_refresh=True)
    assert b.cache_hit is False


def test_build_bundle_ttl_expired(ctx, monkeypatch):
    ctx.write_status("sherry", "# S\n\n## 当前焦点\nx\n")
    _write_config(ctx, {"sherry": {"contextBundle": {"cache_ttl_seconds": 1}}})
    ctx.build_context_bundle("sherry")
    # simulate cache expiry by rewriting expires_at backward
    cp = ctx._cache_path("sherry")
    d = json.loads(cp.read_text())
    d["expires_at"] = time.time() - 5
    cp.write_text(json.dumps(d))
    b2 = ctx.build_context_bundle("sherry")
    assert b2.cache_hit is False


def test_build_bundle_disabled(ctx):
    _write_config(ctx, {"sherry": {"contextBundle": {"enabled": False}}})
    ctx.write_status("sherry", "# S\n\n## 当前焦点\nx\n")
    b = ctx.build_context_bundle("sherry")
    assert b.sources == {}


def test_build_bundle_include_subset(ctx, fake_home):
    ctx.write_status("sherry", "# S\n\n## 当前焦点\nx\n")
    _seed_main_session(fake_home, "sherry", [("user", "hi")])
    _write_config(ctx, {"sherry": {"contextBundle": {"include": ["status"]}}})
    b = ctx.build_context_bundle("sherry")
    assert "status" in b.sources
    assert "main_session" not in b.sources


def test_reset_cache(ctx):
    ctx.write_status("sherry", "# S\n\n## 当前焦点\nx\n")
    ctx.build_context_bundle("sherry")
    assert ctx._cache_path("sherry").exists()
    ctx.reset_cache("sherry")
    assert not ctx._cache_path("sherry").exists()


def test_render_includes_only_present_sources(ctx):
    ctx.write_status("sherry", "# S\n\n## 当前焦点\n只有 status\n")
    b = ctx.build_context_bundle("sherry")
    out = b.render()
    assert "STATUS" in out
    assert "主 session" not in out
    assert "相关记忆" not in out
