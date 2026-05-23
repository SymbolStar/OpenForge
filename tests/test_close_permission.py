"""PR-2: close-thread permission collapse to Scott only.

PRD-v1.0 §3 Rule 6: only Scott closes a thread. Backend enforces by
checking `by == OPERATOR_ID` ("scott") on POST /api/threads/<id>/close.
Other speakers → 403. Missing `by` field → 400.

See also docs/AGENT-COLLAB-OPEN-QUESTIONS.md Q2 for the single-operator
assumption this is built on.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from tests.test_server import _post  # noqa: E402
from tests.test_server_extra import _http  # noqa: E402


def _make_thread(server, squad_id: str = "cp"):
    # squad may already exist from a prior test in this module (server
    # fixture is per-test but file isolation aside, be defensive).
    code, _ = _http(
        "POST", f"{server}/api/squads",
        {"id": squad_id, "members": ["m"], "chair": "m"},
    )
    assert code in (200, 201, 409), code
    t = _post(
        f"{server}/api/squads/{squad_id}/threads",
        {"content": "hello", "created_by": "scott"},
    )
    return t["thread_id"]


# ─── happy path ──────────────────────────────────────────────────────
def test_scott_can_close(server):
    tid = _make_thread(server)
    code, body = _http("POST", f"{server}/api/threads/{tid}/close", {"by": "scott"})
    assert code == 200, body
    payload = json.loads(body)
    assert payload.get("closed_at")
    assert payload.get("closed_by") == "scott"


# ─── 403: other speakers ─────────────────────────────────────────────
def test_agent_cannot_close(server):
    tid = _make_thread(server)
    code, body = _http("POST", f"{server}/api/threads/{tid}/close", {"by": "sherry"})
    assert code == 403
    assert "scott" in body  # error mentions the restriction


def test_judy_cannot_close(server):
    tid = _make_thread(server)
    code, body = _http("POST", f"{server}/api/threads/{tid}/close", {"by": "judy"})
    assert code == 403
    assert "scott" in body


def test_agent_prefixed_id_cannot_close(server):
    tid = _make_thread(server)
    code, body = _http(
        "POST", f"{server}/api/threads/{tid}/close", {"by": "agent:cherry"}
    )
    assert code == 403


def test_empty_by_string_cannot_close(server):
    tid = _make_thread(server)
    code, body = _http("POST", f"{server}/api/threads/{tid}/close", {"by": ""})
    # Empty string is not "scott" → 403 (and definitely not silently defaulted)
    assert code == 403


# ─── 400: missing `by` field ─────────────────────────────────────────
def test_missing_by_field_rejected(server):
    tid = _make_thread(server)
    code, body = _http("POST", f"{server}/api/threads/{tid}/close", {})
    assert code == 400
    assert "by" in body  # error mentions the required field


# ─── 404: unknown thread (preserve existing behavior) ────────────────
def test_unknown_thread_returns_404(server):
    code, body = _http(
        "POST", f"{server}/api/threads/th_dead_beef/close", {"by": "scott"}
    )
    assert code == 404


# ─── legacy field alias still accepted (backward compat) ─────────────
def test_legacy_closed_by_field_still_works(server):
    """Old callers used `closed_by`. We keep accepting it as an alias so
    nothing in the wild breaks while we transition. Same scott check."""
    tid = _make_thread(server)
    code, body = _http(
        "POST", f"{server}/api/threads/{tid}/close", {"closed_by": "scott"}
    )
    assert code == 200

    tid2 = _make_thread(server, squad_id="cp2")
    code, _ = _http(
        "POST", f"{server}/api/threads/{tid2}/close", {"closed_by": "milk"}
    )
    assert code == 403


# ─── idempotency: closing an already-closed thread ──────────────────
def test_double_close_does_not_explode(server):
    """Scott closes, then Scott closes again. Existing store.close_thread
    is idempotent (just updates closed_at/closed_by); make sure the
    permission layer doesn't suddenly start returning 4xx on the second call.
    """
    tid = _make_thread(server)
    code1, _ = _http("POST", f"{server}/api/threads/{tid}/close", {"by": "scott"})
    assert code1 == 200
    code2, body2 = _http("POST", f"{server}/api/threads/{tid}/close", {"by": "scott"})
    assert code2 == 200
    payload = json.loads(body2)
    assert payload.get("closed_by") == "scott"
