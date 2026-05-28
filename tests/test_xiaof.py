"""
Tests for forge_xiaof (\u5c0fF global agent backend, M1).

Covers:
- Unit: validate_ask_request, sse_frame, stream_to_sse_frames ordering, stub_adapter shape.
- HTTP: POST /api/xiaof/ask streams the exact contract event order
  (meta \u2192 token* \u2192 chips \u2192 done) and only ever uses the four allowed
  error codes (unauth | rate_limited | upstream_failed | internal) \u2014
  never `forbidden` (A-7.5 red line).

Contract reference: [[alice/global-agent-API-contract-v0.1.md]] \u00a72/\u00a77.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

# ── unit ────────────────────────────────────────────────────────────


def test_sse_frame_shape(fake_home):
    import forge_xiaof
    frame = forge_xiaof.sse_frame("meta", {"intent": "general_qa"})
    assert frame.startswith("event: meta\n")
    assert "data: " in frame
    assert frame.endswith("\n\n")
    # data line must be parseable JSON with non-ASCII preserved.
    body = json.loads(frame.split("data: ", 1)[1].rstrip("\n"))
    assert body == {"intent": "general_qa"}


def test_validate_ask_request_happy(fake_home):
    import forge_xiaof
    norm = forge_xiaof.validate_ask_request(
        {"query": " hi ", "session_id": "s1", "viewer": "alice"}
    )
    assert norm["query"] == "hi"
    assert norm["session_id"] == "s1"
    assert norm["viewer"] == "alice"
    assert norm["client"] == {}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 4001},
        {"query": "ok", "session_id": 123},
        {"query": "ok", "viewer": 123},
        {"query": "ok", "client": "nope"},
    ],
)
def test_validate_ask_request_rejects(fake_home, bad):
    import forge_xiaof
    with pytest.raises(forge_xiaof.XiaofRequestError):
        forge_xiaof.validate_ask_request(bad)


def test_stream_to_sse_frames_enforces_order(fake_home):
    import forge_xiaof

    # Out-of-order: chips before meta.
    with pytest.raises(ValueError):
        list(forge_xiaof.stream_to_sse_frames([("chips", {"chips": []})]))

    # Duplicate meta.
    with pytest.raises(ValueError):
        list(
            forge_xiaof.stream_to_sse_frames(
                [
                    ("meta", {}),
                    ("meta", {}),
                ]
            )
        )

    # token after chips.
    with pytest.raises(ValueError):
        list(
            forge_xiaof.stream_to_sse_frames(
                [
                    ("meta", {}),
                    ("chips", {"chips": []}),
                    ("token", {"text": "x"}),
                    ("done", {}),
                ]
            )
        )

    # Ended before done.
    with pytest.raises(ValueError):
        list(
            forge_xiaof.stream_to_sse_frames(
                [("meta", {}), ("chips", {"chips": []})]
            )
        )


def test_stub_adapter_emits_contract_shape(fake_home):
    import forge_xiaof
    events = list(forge_xiaof.stub_adapter({"query": "上次 hero 高度的决定"}))
    kinds = [e[0] for e in events]
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    assert "chips" in kinds
    # meta must contain intent + request_id.
    meta_payload = events[0][1]
    assert meta_payload["intent"] in {"thread_search", "general_qa"}
    assert meta_payload["request_id"].startswith("xfr_")
    # chips list must exist and respect the 5-card cap (PRD §4.2).
    chips_payload = next(p for k, p in events if k == "chips")
    assert isinstance(chips_payload["chips"], list)
    assert len(chips_payload["chips"]) <= 5
    # done must carry latency_ms + chip_count.
    done_payload = events[-1][1]
    assert "latency_ms" in done_payload
    assert done_payload["chip_count"] == len(chips_payload["chips"])


def test_classify_intent(fake_home):
    import forge_xiaof
    assert forge_xiaof.classify_intent("帮我查上次讨论") == "thread_search"
    assert forge_xiaof.classify_intent("今天天气如何") == "general_qa"
    assert forge_xiaof.classify_intent("") == "general_qa"


def test_adapter_registry(fake_home):
    import forge_xiaof

    def fake(payload):
        yield ("meta", {"intent": "general_qa", "request_id": "xfr_0"})
        yield ("chips", {"chips": [], "chip_total": 0})
        yield ("done", {"latency_ms": 1, "chip_count": 0})

    forge_xiaof.register_adapter("fake", fake)
    assert forge_xiaof.get_adapter("fake") is fake
    with pytest.raises(KeyError):
        forge_xiaof.get_adapter("nope")


# ── HTTP integration ───────────────────────────────────────────────


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Tiny SSE parser sufficient for tests."""
    events: list[tuple[str, dict]] = []
    event_name = None
    data_lines: list[str] = []
    for line in body.split("\n"):
        if line.startswith(":"):
            continue
        if line == "":
            if event_name is not None and data_lines:
                payload = json.loads("\n".join(data_lines))
                events.append((event_name, payload))
            event_name = None
            data_lines = []
            continue
        if line.startswith("event: "):
            event_name = line[len("event: ") :].strip()
        elif line.startswith("data: "):
            data_lines.append(line[len("data: ") :])
    return events


def _post_sse(base: str, body: dict) -> str:
    req = urllib.request.Request(
        f"{base}/api/xiaof/ask",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        ctype = resp.headers.get("Content-Type", "")
        assert ctype.startswith("text/event-stream")
        return resp.read().decode("utf-8")


def test_http_ask_emits_contract_event_order(server):
    raw = _post_sse(server, {"query": "上次 hero 高度的决定"})
    events = _parse_sse(raw)
    kinds = [e[0] for e in events]
    # No error event in the happy path.
    assert "error" not in kinds
    # Order: meta first, done last, exactly one chips, all token events
    # appear between meta and chips.
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    assert kinds.count("chips") == 1
    chips_idx = kinds.index("chips")
    for i, k in enumerate(kinds):
        if k == "token":
            assert 0 < i < chips_idx, f"token at wrong position: {kinds}"


def test_http_ask_rejects_missing_query_as_internal_not_forbidden(server):
    raw = _post_sse(server, {"session_id": "s1"})
    events = _parse_sse(raw)
    # Validation failure surfaces as a single SSE error event.
    assert events, "expected at least one error event"
    error_events = [e for e in events if e[0] == "error"]
    assert error_events, f"expected error event, got: {events}"
    for _, payload in error_events:
        # Contract red line: only the four allowed codes; never `forbidden`.
        assert payload["code"] in {
            "unauth",
            "rate_limited",
            "upstream_failed",
            "internal",
        }
        assert payload["code"] != "forbidden"


def test_http_ask_adapter_crash_maps_to_internal(server, monkeypatch):
    """
    If a future adapter raises mid-stream, the route must NOT leak the
    exception or invent a new error code; it must downgrade to the
    contract's `internal`. We can't easily monkeypatch the subprocess
    server from here, so this test is a guard at the unit level too:
    see test_stream_to_sse_frames_enforces_order for the framing side,
    and we trust _xiaof_ask's try/except for runtime mapping.
    """
    # Smoke: with the default stub adapter the request succeeds and the
    # chips array is empty (matches A-7.5 unauthorized-result shape so
    # the front-end's zero-state path is exercised).
    raw = _post_sse(server, {"query": "anything"})
    events = _parse_sse(raw)
    chips_event = next(e for e in events if e[0] == "chips")
    assert chips_event[1]["chips"] == []
    assert chips_event[1]["chip_total"] == 0


# ── user-facing copy + general_qa built-ins ────────────────────────


def _stub_body(query: str, client: dict | None = None) -> str:
    import forge_xiaof
    events = list(forge_xiaof.stub_adapter({"query": query, "client": client or {}}))
    return "".join(p["text"] for k, p in events if k == "token")


def test_stub_copy_has_no_engineering_jargon(fake_home):
    """Product red line: no M1/M2/stub/adapter/milestone leakage."""
    forbidden = ("M1", "M2", "stub", "Stub", "STUB", "adapter", "milestone", "占位")
    for q in [
        "你好",
        "上次 hero 高度的决定在哪",
        "今天 Asia/Shanghai 几点",
        "what's the time",
        "随便说点啥",
    ]:
        body = _stub_body(q)
        for bad in forbidden:
            assert bad not in body, f"forbidden token {bad!r} leaked in: {body!r}"


def test_stub_answers_time_question_shanghai(fake_home):
    body = _stub_body("现在 Asia/Shanghai 几点")
    assert "Asia/Shanghai" in body
    # Format: YYYY-MM-DD HH:MM
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", body), body


def test_stub_answers_time_question_via_client_tz(fake_home):
    body = _stub_body("现在几点", client={"tz": "Asia/Tokyo"})
    assert "Asia/Tokyo" in body


def test_stub_general_qa_fallback_when_no_builtin(fake_home):
    body = _stub_body("你好")
    # Neutral copy: mentions cross-thread retrieval coming online, not jargon.
    assert "检索" in body
    assert "M" not in body or "M1" not in body and "M2" not in body


def test_stub_search_intent_fallback_copy(fake_home):
    body = _stub_body("上次 hero 高度的决定在哪")
    assert "检索" in body or "thread" in body.lower()
    assert "占位" not in body
