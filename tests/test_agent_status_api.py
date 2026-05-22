"""HTTP route tests for /api/agents/<id>/status and /context-bundle."""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

import pytest


def _http(method: str, url: str, body: dict | None = None) -> tuple[int, dict | bytes]:
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body  # type: ignore[return-value]


def test_status_post_get(server):
    code, body = _http("POST", f"{server}/api/agents/sherry/status", {
        "content": "# Sherry STATUS\n\n## 当前焦点\n做日报\n",
    })
    assert code == 200, body
    assert body["agent"] == "sherry"

    code, body = _http("GET", f"{server}/api/agents/sherry/status")
    assert code == 200
    assert "做日报" in body["content"]


def test_status_post_bad_payload(server):
    code, body = _http("POST", f"{server}/api/agents/sherry/status", {})
    assert code == 400
    assert "error" in body


def test_status_post_invalid_agent(server):
    code, body = _http("POST", f"{server}/api/agents/.../status", {"content": "x"})
    # the route regex won't match '...' → 404 from server
    assert code in (400, 404)


def test_status_get_missing(server):
    code, body = _http("GET", f"{server}/api/agents/ghost/status")
    assert code == 404


def test_status_patch_section(server):
    _http("POST", f"{server}/api/agents/sherry/status", {
        "content": "# Sherry\n\n## 当前焦点\n旧\n\n## 已知 blocker\n无\n",
    })
    code, body = _http("PATCH", f"{server}/api/agents/sherry/status", {
        "section": "当前焦点",
        "content": "16:35 已入箱 ✅",
    })
    assert code == 200, body
    code, body = _http("GET", f"{server}/api/agents/sherry/status")
    assert "16:35 已入箱" in body["content"]
    assert "## 已知 blocker" in body["content"]


def test_status_patch_missing_section(server):
    _http("POST", f"{server}/api/agents/sherry/status", {
        "content": "# Sherry\n\n## 当前焦点\nx\n",
    })
    code, body = _http("PATCH", f"{server}/api/agents/sherry/status", {
        "section": "不存在",
        "content": "x",
    })
    assert code == 404


def test_status_patch_no_status_yet(server):
    code, body = _http("PATCH", f"{server}/api/agents/sherry/status", {
        "section": "当前焦点",
        "content": "x",
    })
    assert code == 404


def test_status_patch_bad_payload(server):
    code, body = _http("PATCH", f"{server}/api/agents/sherry/status", {"section": "  "})
    assert code == 400


def test_context_bundle_empty_when_no_sources(server):
    code, body = _http("GET", f"{server}/api/agents/sherry/context-bundle")
    assert code == 200
    assert body["sources"] == {}
    assert body["rendered"] == ""


def test_context_bundle_after_status_write(server):
    _http("POST", f"{server}/api/agents/sherry/status", {
        "content": "# Sherry\n\n## 当前焦点\n16:35 已入箱\n",
    })
    code, body = _http("GET", f"{server}/api/agents/sherry/context-bundle?refresh=1")
    assert code == 200
    assert "status" in body["sources"]
    assert "16:35 已入箱" in body["rendered"]


def test_context_bundle_refresh_flag(server):
    _http("POST", f"{server}/api/agents/sherry/status", {
        "content": "# Sherry\n\n## 当前焦点\na\n",
    })
    # warm cache
    _http("GET", f"{server}/api/agents/sherry/context-bundle")
    # second call without refresh → cache_hit True
    code, body = _http("GET", f"{server}/api/agents/sherry/context-bundle")
    assert body["cache_hit"] is True
    code, body = _http("GET", f"{server}/api/agents/sherry/context-bundle?refresh=1")
    assert body["cache_hit"] is False
