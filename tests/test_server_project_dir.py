"""HTTP-level tests for v0.5 PR-A: /api/fs/validate + project_dir on /api/squads.

Covers PRD A-10/A-11 behaviour:
  * GET /api/fs/validate happy path (repo + non-repo + missing)
  * 400 on relative / missing path
  * POST /api/squads accepts project_dir and surfaces project_dir_valid
  * PATCH /api/squads/<id> sets project_dir; GET reflects validity flag
  * PATCH rejects non-absolute project_dir
"""
# ruff: noqa: F811
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request

import pytest

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


# ─── /api/fs/validate ────────────────────────────────────────────────

def test_fs_validate_requires_path(server):
    code, body = _request("GET", f"{server}/api/fs/validate")
    assert code == 400
    assert "error" in body


def test_fs_validate_rejects_relative_path(server):
    code, body = _request("GET", f"{server}/api/fs/validate?path=relative/foo")
    assert code == 400
    assert "absolute" in body.get("error", "").lower()


def test_fs_validate_nonexistent(server, tmp_path):
    target = tmp_path / "does-not-exist"
    code, body = _request("GET", f"{server}/api/fs/validate?path={target}")
    assert code == 200
    assert body == {"exists": False, "is_git_repo": False, "error": None}


def test_fs_validate_non_git_dir(server, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    code, body = _request("GET", f"{server}/api/fs/validate?path={plain}")
    assert code == 200
    assert body["exists"] is True
    assert body["is_git_repo"] is False


def test_fs_validate_git_repo(server, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    code, body = _request("GET", f"{server}/api/fs/validate?path={repo}")
    assert code == 200
    assert body == {"exists": True, "is_git_repo": True, "error": None}


# ─── POST /api/squads with project_dir ──────────────────────────────

def test_create_squad_with_project_dir_returns_validity(server, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    code, body = _request("POST", f"{server}/api/squads", {
        "id": "with-repo",
        "name": "x",
        "members": ["scott"],
        "chair": "scott",
        "project_dir": str(repo),
    })
    assert code == 201, body
    assert body["project_dir"] == str(repo)
    assert body["project_dir_valid"] is True


def test_create_squad_rejects_relative_project_dir(server):
    code, body = _request("POST", f"{server}/api/squads", {
        "id": "bad-path",
        "name": "x",
        "members": ["scott"],
        "chair": "scott",
        "project_dir": "relative/path",
    })
    assert code == 400
    assert "absolute" in body.get("error", "").lower()


def test_create_squad_without_project_dir_has_null_validity(server):
    code, body = _request("POST", f"{server}/api/squads", {
        "id": "no-repo",
        "name": "x",
        "members": ["scott"],
        "chair": "scott",
    })
    assert code == 201
    assert body["project_dir"] is None
    assert body["project_dir_valid"] is None


# ─── PATCH /api/squads/<id> ─────────────────────────────────────────

def test_patch_squad_sets_project_dir_and_surfaces_valid(server, tmp_path):
    repo = tmp_path / "patched-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    # bootstrap squad without project_dir
    code, _ = _request("POST", f"{server}/api/squads", {
        "id": "patched", "name": "p", "members": ["scott"], "chair": "scott",
    })
    assert code == 201
    code, body = _request("PATCH", f"{server}/api/squads/patched",
                          {"project_dir": str(repo)})
    assert code == 200
    assert body["project_dir"] == str(repo)
    assert body["project_dir_valid"] is True

    # GET should also reflect the validity flag
    code, full = _request("GET", f"{server}/api/squads/patched")
    assert code == 200
    assert full["squad"]["project_dir"] == str(repo)
    assert full["squad"]["project_dir_valid"] is True


def test_patch_squad_rejects_relative_project_dir(server):
    code, _ = _request("POST", f"{server}/api/squads", {
        "id": "patch-bad", "name": "x", "members": ["scott"], "chair": "scott",
    })
    assert code == 201
    code, body = _request("PATCH", f"{server}/api/squads/patch-bad",
                          {"project_dir": "not/absolute"})
    assert code == 400
    assert "absolute" in body.get("error", "").lower()


def test_patch_squad_clears_project_dir(server, tmp_path):
    repo = tmp_path / "clearme"
    repo.mkdir()
    (repo / ".git").mkdir()
    _request("POST", f"{server}/api/squads", {
        "id": "clearable", "name": "c", "members": ["scott"], "chair": "scott",
        "project_dir": str(repo),
    })
    code, body = _request("PATCH", f"{server}/api/squads/clearable",
                          {"project_dir": ""})
    assert code == 200
    assert body["project_dir"] is None
    assert body["project_dir_valid"] is None


def test_list_squads_includes_project_dir_valid(server, tmp_path):
    repo = tmp_path / "listed"
    repo.mkdir()
    (repo / ".git").mkdir()
    _request("POST", f"{server}/api/squads", {
        "id": "listed", "name": "L", "members": ["scott"], "chair": "scott",
        "project_dir": str(repo),
    })
    code, lst = _request("GET", f"{server}/api/squads")
    assert code == 200
    match = next((s for s in lst if s["id"] == "listed"), None)
    assert match is not None
    assert match["project_dir_valid"] is True
