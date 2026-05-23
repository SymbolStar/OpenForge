"""HTTP-level tests for /api/squads — focus on id validation and route round-trip.

Regression for the bug where the front-end accepted `ai-research` (per the
URL route regex `[\\w-]{1,32}`) but server-side `create_squad` validation used
`^\\w{1,32}$`, which rejected hyphens and surfaced as "点 Create 没反应".
"""
# ruff: noqa: F811  (pytest fixture re-export shadows the import)
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

# Re-export shared server fixture
from tests.conftest import server  # noqa: F401, E402


def _request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            payload = r.read().decode("utf-8")
            return r.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(payload)
        except json.JSONDecodeError:
            return e.code, {"_raw": payload}


def _post(url: str, body: dict) -> tuple[int, dict]:
    return _request("POST", url, body)


def _get(url: str) -> tuple[int, dict]:
    return _request("GET", url)


def _patch(url: str, body: dict) -> tuple[int, dict]:
    return _request("PATCH", url, body)


def _delete(url: str) -> tuple[int, dict]:
    return _request("DELETE", url)


# ─── id validation: positive cases ─────────────────────────────────────

@pytest.mark.parametrize("squad_id", [
    "frontend",          # baseline alnum
    "ai-research",       # hyphen — the actual regression case
    "team_42",           # underscore + digit
    "a",                 # single char
    "Team-A_b9",         # mixed
    "x" * 32,            # max length
])
def test_create_squad_accepts_valid_ids(server, squad_id):
    code, body = _post(f"{server}/api/squads", {
        "id": squad_id,
        "name": squad_id,
        "members": ["scott"],
        "chair": "scott",
    })
    assert code == 201, f"expected 201 for id={squad_id!r}, got {code}: {body}"
    assert body["id"] == squad_id


# ─── id validation: negative cases ─────────────────────────────────────

@pytest.mark.parametrize("squad_id", [
    "-leading-dash",     # must start with alnum
    "_leading-under",    # must start with alnum (\w allows _, but we explicitly disallow leading _ to prevent shell flag confusion)
    "",                  # empty
    "x" * 33,            # too long
    "has space",         # space
    "has.dot",           # dot
    "中文",               # CJK not in \w in our pattern
    "drop;table",        # punctuation
    "../etc",            # path traversal attempt
])
def test_create_squad_rejects_invalid_ids(server, squad_id):
    code, body = _post(f"{server}/api/squads", {
        "id": squad_id,
        "name": "x",
        "members": ["scott"],
        "chair": "scott",
    })
    assert code == 400, f"expected 400 for id={squad_id!r}, got {code}: {body}"
    assert "error" in body
    # Error message must mention the rule so users can self-correct
    assert any(tok in body["error"] for tok in ("字符", "char", "id"))


def test_create_squad_rejects_non_string_id(server):
    code, body = _post(f"{server}/api/squads", {
        "id": 12345,
        "name": "x",
        "members": ["scott"],
        "chair": "scott",
    })
    assert code == 400
    assert "error" in body


def test_create_squad_rejects_duplicate(server):
    p = {"id": "dup-team", "name": "x", "members": ["scott"], "chair": "scott"}
    code, _ = _post(f"{server}/api/squads", p)
    assert code == 201
    code2, body2 = _post(f"{server}/api/squads", p)
    assert code2 in (400, 409), f"expected duplicate to fail, got {code2}: {body2}"


# ─── round-trip with hyphenated id: GET / PATCH / DELETE all work ─────

def test_hyphen_id_full_crud_roundtrip(server):
    sid = "ai-research"
    code, created = _post(f"{server}/api/squads", {
        "id": sid,
        "name": "SS-AI-Research",
        "description": "research arm",
        "emoji": "🧠",
        "members": ["scott"],
        "chair": "scott",
    })
    assert code == 201

    # GET single
    code, got = _get(f"{server}/api/squads/{sid}")
    assert code == 200
    assert got["squad"]["id"] == sid
    assert got["squad"]["name"] == "SS-AI-Research"

    # GET list contains it
    code, lst = _get(f"{server}/api/squads")
    assert code == 200
    assert any(s["id"] == sid for s in lst)

    # PATCH (description update)
    code, patched = _patch(f"{server}/api/squads/{sid}", {"description": "updated"})
    assert code == 200, f"PATCH failed: {patched}"

    code, got2 = _get(f"{server}/api/squads/{sid}")
    assert got2["squad"]["description"] == "updated"

    # DELETE
    code, _ = _delete(f"{server}/api/squads/{sid}")
    assert code in (200, 204)

    code, got3 = _get(f"{server}/api/squads/{sid}")
    assert code == 404


# ─── route regex vs create regex must agree ───────────────────────────

def test_route_regex_and_create_regex_agree_on_hyphen(server):
    """If POST accepts an id, GET on that id must reach the resource (not 404).

    This is the meta-regression: SQUAD_ID_RE (create) and SQUAD_ROUTE_RE (URL)
    used to disagree on `-`, causing weird half-broken states.
    """
    sid = "x-y_z9"
    code, _ = _post(f"{server}/api/squads", {
        "id": sid, "name": sid, "members": ["scott"], "chair": "scott",
    })
    assert code == 201
    code, got = _get(f"{server}/api/squads/{sid}")
    assert code == 200, "URL route must accept any id the create endpoint accepted"
    assert got["squad"]["id"] == sid
