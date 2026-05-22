"""Unit tests for forge_session_search.

Covers PRD §6.1 plus a few extras:
  - empty sessions dir → 0 hits
  - case-insensitive substring on a real message line
  - days filter excludes old hits (and includes recent ones)
  - scope=main excludes forge-* / standup-* / huddle-*
  - scope=forge / scope=all
  - trajectory.jsonl + .bak + .reset files are excluded
  - large-file (>50MB) is skipped with a warning
  - query length validation
  - bad json lines are skipped (resilience)
  - tool-call parts and string content shapes
  - max_hits + total_hits + truncated flag
  - snippet centered on hit, char_offset > 0
  - days=0 = no time window
  - invalid args raise SessionSearchError
"""
from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timezone
from pathlib import Path

import pytest

import forge_session_search as fss
from forge_session_search import SessionSearchError, search

# ──────────────────────────── helpers ────────────────────────────


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _write_session(
    sessions_dir: Path,
    stem: str,
    lines: list[dict],
    *,
    mtime: float | None = None,
) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    p = sessions_dir / f"{stem}.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _msg(role: str, text: str, *, ts: float) -> dict:
    return {
        "type": "message",
        "id": f"msg_{int(ts * 1000)}",
        "timestamp": _iso(ts),
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _msg_string_content(role: str, text: str, *, ts: float) -> dict:
    return {
        "type": "message",
        "id": f"msg_str_{int(ts * 1000)}",
        "timestamp": _iso(ts),
        "message": {"role": role, "content": text},
    }


def _msg_toolcall(name: str, args: dict, *, ts: float) -> dict:
    return {
        "type": "message",
        "id": f"msg_tc_{int(ts * 1000)}",
        "timestamp": _iso(ts),
        "message": {
            "role": "assistant",
            "content": [{"type": "toolCall", "name": name, "arguments": args}],
        },
    }


@pytest.fixture
def agent_dir(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    d = home / ".openclaw" / "agents" / "alice" / "sessions"
    d.mkdir(parents=True)
    return d


# ──────────────────────────── tests ────────────────────────────


def test_empty_sessions_dir(agent_dir):
    res = search("alice", "anything")
    assert res["total_hits"] == 0
    assert res["hits"] == []
    assert res["searched_sessions"] == 0
    assert res["scope"] == "main"


def test_missing_agent_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    res = search("ghost", "x")
    assert res["total_hits"] == 0
    assert res["searched_sessions"] == 0


def test_main_session_hit_case_insensitive(agent_dir):
    now = time.time()
    _write_session(
        agent_dir,
        "26252f48-9aae-49a9-aedb-45f9d9d9a32c",
        [
            {"type": "session", "id": "26252f48", "timestamp": _iso(now - 60)},
            _msg(
                "assistant",
                "已完成 mustafa-email-archive 子 agent 归档，103 封邮件分类完毕。",
                ts=now - 30,
            ),
            _msg("user", "ok", ts=now - 10),
        ],
    )
    res = search("alice", "MUSTAFA")
    assert res["total_hits"] == 1
    h = res["hits"][0]
    assert "mustafa-email-archive" in h["snippet"]
    assert h["role"] == "assistant"
    assert h["session_kind"] == "main"
    assert h["char_offset"] >= 0


def test_string_content_shape(agent_dir):
    now = time.time()
    _write_session(
        agent_dir,
        "abc",
        [_msg_string_content("user", "find the mustafa thread please", ts=now - 5)],
    )
    res = search("alice", "mustafa")
    assert res["total_hits"] == 1
    assert "mustafa" in res["hits"][0]["snippet"].lower()


def test_toolcall_content_searchable(agent_dir):
    now = time.time()
    _write_session(
        agent_dir,
        "abc",
        [_msg_toolcall("exec", {"command": "ls /tmp/mustafa-email"}, ts=now - 5)],
    )
    res = search("alice", "mustafa-email")
    assert res["total_hits"] == 1


def test_days_filter_excludes_old(agent_dir):
    now = time.time()
    old = now - 10 * 86400
    _write_session(
        agent_dir,
        "old",
        [_msg("assistant", "mustafa from a long time ago", ts=old)],
        mtime=old,
    )
    res = search("alice", "mustafa", days=5)
    assert res["total_hits"] == 0
    # days=0 = no window
    res_all = search("alice", "mustafa", days=0)
    assert res_all["total_hits"] == 1


def test_days_filter_keeps_recent(agent_dir):
    now = time.time()
    _write_session(
        agent_dir,
        "recent",
        [_msg("assistant", "mustafa today", ts=now - 3600)],
    )
    res = search("alice", "mustafa", days=1)
    assert res["total_hits"] == 1


def test_scope_main_excludes_forge(agent_dir):
    now = time.time()
    _write_session(
        agent_dir, "main-1", [_msg("user", "needle in main", ts=now - 10)],
    )
    _write_session(
        agent_dir, "forge-test", [_msg("user", "needle in forge", ts=now - 10)],
    )
    _write_session(
        agent_dir, "standup-x", [_msg("user", "needle in standup", ts=now - 10)],
    )
    _write_session(
        agent_dir, "huddle-y", [_msg("user", "needle in huddle", ts=now - 10)],
    )
    res = search("alice", "needle", scope="main")
    assert res["total_hits"] == 1
    assert res["hits"][0]["session_id"] == "main-1"
    assert res["hits"][0]["session_kind"] == "main"


def test_scope_forge_only(agent_dir):
    now = time.time()
    _write_session(agent_dir, "main-1", [_msg("user", "needle main", ts=now - 10)])
    _write_session(agent_dir, "forge-a", [_msg("user", "needle forge a", ts=now - 10)])
    _write_session(agent_dir, "forge-b", [_msg("user", "needle forge b", ts=now - 20)])
    res = search("alice", "needle", scope="forge")
    assert res["total_hits"] == 2
    assert all(h["session_kind"] == "forge" for h in res["hits"])


def test_scope_all(agent_dir):
    now = time.time()
    _write_session(agent_dir, "main-1", [_msg("user", "needle main", ts=now - 10)])
    _write_session(agent_dir, "forge-a", [_msg("user", "needle forge", ts=now - 20)])
    res = search("alice", "needle", scope="all")
    assert res["total_hits"] == 2


def test_trajectory_and_backup_files_excluded(agent_dir):
    now = time.time()
    # Real session
    _write_session(agent_dir, "main", [_msg("user", "needle main", ts=now - 5)])
    # Trajectory: must be excluded
    (agent_dir / "main.trajectory.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "timestamp": _iso(now - 5),
                "message": {"role": "user", "content": "needle trajectory"},
            }
        )
        + "\n"
    )
    # Backup
    (agent_dir / "main.jsonl.bak-12345-67890").write_text(
        json.dumps(
            {
                "type": "message",
                "timestamp": _iso(now - 5),
                "message": {"role": "user", "content": "needle backup"},
            }
        )
        + "\n"
    )
    # Reset artifact
    (agent_dir / "main.jsonl.reset.2026-05-09T01-27-01.264Z").write_text(
        json.dumps(
            {
                "type": "message",
                "timestamp": _iso(now - 5),
                "message": {"role": "user", "content": "needle reset"},
            }
        )
        + "\n"
    )
    res = search("alice", "needle", scope="all")
    assert res["total_hits"] == 1
    assert res["hits"][0]["session_id"] == "main"


def test_max_hits_caps_and_total_reports_full(agent_dir):
    now = time.time()
    msgs = [_msg("user", f"needle #{i}", ts=now - i) for i in range(5)]
    _write_session(agent_dir, "main", msgs)
    res = search("alice", "needle", max_hits=2)
    assert res["total_hits"] == 5
    assert res["returned_hits"] == 2
    assert len(res["hits"]) == 2
    assert res["truncated"] is True


def test_hits_sorted_newest_first(agent_dir):
    now = time.time()
    _write_session(
        agent_dir,
        "main",
        [
            _msg("user", "needle older", ts=now - 100),
            _msg("user", "needle newer", ts=now - 10),
            _msg("user", "needle middle", ts=now - 50),
        ],
    )
    res = search("alice", "needle", max_hits=10)
    snippets = [h["snippet"] for h in res["hits"]]
    assert snippets[0].endswith("needle newer") or "newer" in snippets[0]
    assert "older" in snippets[-1]


def test_snippet_truncated_to_max(agent_dir):
    now = time.time()
    long_text = "x" * 800 + " mustafa " + "y" * 800
    _write_session(agent_dir, "main", [_msg("user", long_text, ts=now - 5)])
    res = search("alice", "mustafa")
    h = res["hits"][0]
    # Snippet shouldn't be the full 1600+ chars.
    assert len(h["snippet"]) <= fss.SNIPPET_MAX + 1  # +1 for trailing ellipsis


def test_large_file_skipped_with_warning(agent_dir, monkeypatch):
    """50MB+ files are skipped; warning recorded."""
    now = time.time()
    p = _write_session(agent_dir, "main", [_msg("user", "needle here", ts=now - 5)])

    # Patch stat().st_size via wrapper class — much cleaner than mocking os.stat.
    real_stat = Path.stat
    fake_size = fss.MAX_SESSION_BYTES + 1

    def fake_stat(self, *, follow_symlinks=True):
        st = real_stat(self, follow_symlinks=follow_symlinks)
        if self == p:
            class _S:
                st_size = fake_size
                st_mtime = st.st_mtime
            return _S()
        return st

    monkeypatch.setattr(Path, "stat", fake_stat)
    res = search("alice", "needle")
    assert res["total_hits"] == 0
    assert any("skipped" in w for w in res["warnings"])


def test_query_too_long_raises():
    with pytest.raises(SessionSearchError):
        search("alice", "x" * (fss.MAX_QUERY_LEN + 1))


def test_empty_query_raises():
    with pytest.raises(SessionSearchError):
        search("alice", "")
    with pytest.raises(SessionSearchError):
        search("alice", "   ")


def test_bad_args_raise():
    with pytest.raises(SessionSearchError):
        search("", "x")
    with pytest.raises(SessionSearchError):
        search("alice", "x", days=-1)
    with pytest.raises(SessionSearchError):
        search("alice", "x", max_hits=0)
    with pytest.raises(SessionSearchError):
        search("alice", "x", scope="bogus")
    with pytest.raises(SessionSearchError):
        search("alice", 123)  # type: ignore[arg-type]


def test_max_hits_capped_to_abs_max(agent_dir):
    now = time.time()
    msgs = [_msg("u", f"needle{i}", ts=now - i) for i in range(60)]
    _write_session(agent_dir, "main", msgs)
    res = search("alice", "needle", max_hits=999)
    assert res["returned_hits"] <= fss.ABS_MAX_HITS


def test_bad_json_lines_skipped(agent_dir):
    now = time.time()
    p = agent_dir / "main.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write("{broken json\n")
        fh.write(json.dumps(_msg("user", "needle good", ts=now - 5)) + "\n")
        fh.write("[]\n")  # not a dict
        fh.write(json.dumps({"type": "not-message"}) + "\n")
    res = search("alice", "needle")
    assert res["total_hits"] == 1


def test_messages_without_text_skipped(agent_dir):
    now = time.time()
    _write_session(
        agent_dir,
        "main",
        [
            {
                "type": "message",
                "timestamp": _iso(now - 5),
                "message": {"role": "user", "content": [{"type": "image"}]},
            },
            _msg("user", "needle present", ts=now - 3),
        ],
    )
    res = search("alice", "needle")
    assert res["total_hits"] == 1


def test_session_missing_timestamp_still_returns(agent_dir):
    """A message without timestamp shouldn't crash; days filter just lets it through."""
    now = time.time()
    p = agent_dir / "main.jsonl"
    p.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {"role": "user", "content": "needle no ts"},
            }
        )
        + "\n"
    )
    os.utime(p, (now, now))
    res = search("alice", "needle", days=0)
    assert res["total_hits"] == 1


def test_extract_text_handles_weird_shapes():
    # Direct unit on the helper for resilience.
    assert fss._extract_text(None) == ""
    assert fss._extract_text(42) == ""  # type: ignore[arg-type]
    assert fss._extract_text("plain") == "plain"
    assert fss._extract_text(["a", "b"]) == "a\nb"
    out = fss._extract_text(
        [
            {"type": "text", "text": "hello"},
            {"type": "toolCall", "name": "x", "arguments": {"k": "v"}},
            {"type": "toolResult", "result": "ok"},
            {"type": "toolResult", "result": {"a": 1}},
            {"type": "mystery", "blob": 1},
        ]
    )
    assert "hello" in out
    assert "tool:x" in out
    assert "ok" in out
    assert '"a": 1' in out


def test_parse_iso_handles_z_and_offset():
    assert fss._parse_iso(None) is None
    assert fss._parse_iso("") is None
    assert fss._parse_iso("garbage") is None
    assert fss._parse_iso("2026-05-20T01:58:22.047Z") is not None
    assert fss._parse_iso("2026-05-20T01:58:22+00:00") is not None


def test_session_cap(agent_dir, monkeypatch):
    """More than MAX_SESSIONS files → only newest MAX_SESSIONS scanned."""
    monkeypatch.setattr(fss, "MAX_SESSIONS", 3)
    now = time.time()
    # 5 sessions; only the 3 newest should be searched.
    for i in range(5):
        _write_session(
            agent_dir,
            f"main-{i}",
            [_msg("user", "needle", ts=now - i * 10)],
            mtime=now - i * 10,
        )
    res = search("alice", "needle", days=0)
    assert res["searched_sessions"] == 3
    assert res["total_hits"] == 3
    assert any("only the 3 most-recent" in w for w in res["warnings"])


def test_timeout_returns_partial(agent_dir, monkeypatch):
    """Deadline mid-scan → partial result, warning recorded, no crash."""
    monkeypatch.setattr(fss, "TOTAL_TIMEOUT_S", 0.0)
    now = time.time()
    _write_session(agent_dir, "main", [_msg("user", "needle", ts=now - 5)])
    res = search("alice", "needle")
    assert res["timed_out"] is True
    assert any("global timeout" in w for w in res["warnings"])


def test_scan_aborts_inside_file(agent_dir, monkeypatch):
    """A deadline that elapses while reading mid-file produces the per-file warning."""
    now = time.time()
    msgs = [_msg("user", f"needle {i}", ts=now - i) for i in range(50)]
    _write_session(agent_dir, "main", msgs)

    real_monotonic = time.monotonic
    state = {"calls": 0}

    def fake_monotonic():
        state["calls"] += 1
        # Let setup pass, then jump past the deadline.
        if state["calls"] > 3:
            return real_monotonic() + fss.TOTAL_TIMEOUT_S + 10
        return real_monotonic()

    monkeypatch.setattr("forge_session_search.time.monotonic", fake_monotonic)
    res = search("alice", "needle")
    # Either partial scan inside one file (per-file warning) or global-timeout warning.
    assert res["timed_out"] or any("scan aborted" in w for w in res["warnings"])


def test_returned_dict_shape(agent_dir):
    """Sanity-check the API surface matches the PRD §4.1 example."""
    now = time.time()
    _write_session(agent_dir, "main", [_msg("user", "needle here", ts=now - 5)])
    res = search("alice", "needle")
    for k in [
        "agent",
        "query",
        "scope",
        "days_window",
        "searched_sessions",
        "total_hits",
        "returned_hits",
        "hits",
        "truncated",
        "timed_out",
        "warnings",
    ]:
        assert k in res, f"missing key: {k}"
    h = res["hits"][0]
    for k in ["session_id", "session_kind", "ts", "role", "snippet", "char_offset"]:
        assert k in h, f"hit missing key: {k}"
