"""
Tests for the OpenAI-compatible adapter + default routing.

Covers:
- Jargon scrubbing on streamed LLM output (incl. split-across-chunks).
- env-gated enable; missing env → routes to stub, never crashes.
- Routing: time query → local builtin (no LLM); open general_qa with env
  → openai adapter; thread_search → stub even with env set.
- Upstream failure → XiaofRequestError code='upstream_failed' (never
  'forbidden', A-7.5 red line preserved).
- Event order meta → token* → chips → done holds for the LLM path.
- Empty chips on LLM path (LLM cannot bypass A-7 retrieval ACL gate).
"""

from __future__ import annotations

import io
import json

import pytest


@pytest.fixture
def openai_env(monkeypatch):
    monkeypatch.setenv("XIAOF_OPENAI_BASE_URL", "https://fake.test/v1")
    monkeypatch.setenv("XIAOF_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("XIAOF_OPENAI_MODEL", "test-model")


def _sse_chunk(content: str) -> bytes:
    payload = {"choices": [{"delta": {"content": content}}]}
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _fake_response(chunks: list[str]):
    body = b"".join(_sse_chunk(c) for c in chunks) + b"data: [DONE]\n"

    class FakeResp(io.BytesIO):
        def close(self):  # noqa: D401
            super().close()

        def __iter__(self):
            data = self.getvalue().splitlines(keepends=True)
            yield from data

    return FakeResp(body)


# ── jargon scrubber ────────────────────────────────────────────────


def test_scrub_jargon_basic(fake_home):
    import forge_xiaof_openai as oa
    assert oa.scrub_jargon("hello M1 world") == "hello ·· world"
    assert oa.scrub_jargon("调用 stub 走 adapter") == "调用 ···· 走 ·······"
    # Words we don't scrub are left alone.
    assert oa.scrub_jargon("just a plain answer") == "just a plain answer"


def test_split_emit_buffer_keeps_lookahead(fake_home):
    import forge_xiaof_openai as oa
    emit, keep = oa._split_emit_buffer("the milestone is here")
    assert "milestone" not in emit  # would have been split
    assert keep.endswith("here") or "milestone" in keep + emit


# ── env gating ─────────────────────────────────────────────────────


def test_openai_disabled_without_env(fake_home, monkeypatch):
    monkeypatch.delenv("XIAOF_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("XIAOF_OPENAI_API_KEY", raising=False)
    import forge_xiaof_openai as oa
    assert oa.openai_enabled() is False


def test_openai_enabled_with_env(fake_home, openai_env):
    import forge_xiaof_openai as oa
    assert oa.openai_enabled() is True


# ── routing ────────────────────────────────────────────────────────


def test_default_adapter_routes_time_query_to_stub(fake_home, openai_env, monkeypatch):
    """Time query must use local zoneinfo, never hit the LLM."""
    import forge_xiaof
    import forge_xiaof_openai as oa

    called = {"n": 0}

    def _boom(req, timeout):
        called["n"] += 1
        raise AssertionError("LLM must not be called for time queries")

    monkeypatch.setattr(oa, "_urlopen", _boom)
    events = list(forge_xiaof.default_adapter({"query": "现在 Asia/Shanghai 几点"}))
    assert called["n"] == 0
    body = "".join(p["text"] for k, p in events if k == "token")
    assert "Asia/Shanghai" in body


def test_default_adapter_routes_thread_search_to_stub(fake_home, openai_env, monkeypatch):
    """thread_search MUST stay on stub even with LLM env present —
    LLM path cannot bypass A-7 retrieval ACL gate."""
    import forge_xiaof
    import forge_xiaof_openai as oa

    def _boom(req, timeout):
        raise AssertionError("LLM must not be called for thread_search")

    monkeypatch.setattr(oa, "_urlopen", _boom)
    events = list(forge_xiaof.default_adapter({"query": "上次 hero 高度的决定在哪条 post"}))
    chips = next(p for k, p in events if k == "chips")
    assert chips["chips"] == []


def test_default_adapter_routes_open_qa_to_llm(fake_home, openai_env, monkeypatch):
    """Open-ended general_qa with env present → real LLM adapter."""
    import forge_xiaof
    import forge_xiaof_openai as oa

    captured = {}

    def _fake(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return _fake_response(["你好，", "我是回答。"])

    monkeypatch.setattr(oa, "_urlopen", _fake)

    events = list(forge_xiaof.default_adapter({"query": "讲个冷笑话"}))
    kinds = [k for k, _ in events]
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    assert kinds.count("chips") == 1
    assert "token" in kinds
    body = "".join(p["text"] for k, p in events if k == "token")
    assert body == "你好，我是回答。"
    chips = next(p for k, p in events if k == "chips")
    assert chips["chips"] == []  # LLM never produces chips
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer test-key"


# ── jargon scrubber on LLM output ──────────────────────────────────


def test_llm_output_jargon_scrubbed(fake_home, openai_env, monkeypatch):
    import forge_xiaof_openai as oa

    monkeypatch.setattr(
        oa,
        "_urlopen",
        lambda req, timeout: _fake_response(["这次返回的 M1 stub 是个 milestone", " 占位文本"]),
    )

    events = list(oa.openai_compatible_adapter({"query": "test"}))
    body = "".join(p["text"] for k, p in events if k == "token")
    for bad in ("M1", "stub", "milestone", "占位", "adapter"):
        assert bad not in body, f"forbidden word {bad!r} leaked in: {body!r}"


def test_llm_output_jargon_scrubbed_across_chunk_boundary(fake_home, openai_env, monkeypatch):
    """A forbidden word split across two upstream chunks must still be
    caught — that's the entire point of _LOOKAHEAD."""
    import forge_xiaof_openai as oa

    # "milestone" split as "mile" + "stone".
    monkeypatch.setattr(
        oa,
        "_urlopen",
        lambda req, timeout: _fake_response(["hello mile", "stone tail"]),
    )
    events = list(oa.openai_compatible_adapter({"query": "x"}))
    body = "".join(p["text"] for k, p in events if k == "token")
    assert "milestone" not in body.lower()


# ── upstream failures ──────────────────────────────────────────────


def test_upstream_failure_maps_to_upstream_failed(fake_home, openai_env, monkeypatch):
    import forge_xiaof_openai as oa
    from forge_xiaof import XiaofRequestError

    def _broken(req, timeout):
        raise ConnectionError("boom")

    monkeypatch.setattr(oa, "_urlopen", _broken)

    gen = oa.openai_compatible_adapter({"query": "anything"})
    # First event (meta) is fine; the network call happens on next().
    first_kind, _ = next(gen)
    assert first_kind == "meta"
    with pytest.raises(XiaofRequestError) as exc:
        next(gen)
    assert exc.value.code == "upstream_failed"
    # A-7.5: must NEVER produce `forbidden`.
    assert exc.value.code != "forbidden"
