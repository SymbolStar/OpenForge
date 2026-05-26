"""forge_project.py — project_dir validation (shared between server + router).

OpenForge v0.5 PR-A introduced ``squad.project_dir``: an optional absolute path
that pins a squad to a target git repo. Multiple call sites need to know
whether a given path is "currently a real git repo": the squad API (for the
derived ``project_dir_valid`` field), and the post router (for deciding
whether to inject the ``[project]`` segment into agent context bundles).

This module gives both a single source of truth + 60s TTL cache, so a busy
squad list view doesn't fan out to N filesystem stats and so router-side
context-bundle building stays cheap on every routed @mention.

Public API:

* :func:`validate(path)`  → ``{"exists": bool, "is_git_repo": bool, "error": str|None}``
* :func:`invalidate(*paths)` — drop cache for specific paths (or all if no args)
* :func:`derive_validity(project_dir)` → ``True | False | None`` for UI / preamble use
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

_TTL = 60.0
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def validate(path: str) -> dict:
    """Return ``{exists, is_git_repo, error}`` for an absolute path. Cached 60s.

    Defensive against traversal: resolves the path through ``Path.resolve``
    (without requiring existence) before stat'ing, so ``//`` and ``/./`` and
    symlink games normalize before we touch the FS. Never raises; on any
    unexpected error returns ``{exists: false, is_git_repo: false, error: <msg>}``.
    """
    now = time.time()
    with _lock:
        hit = _cache.get(path)
        if hit and (now - hit[0]) < _TTL:
            return hit[1]
    try:
        p = Path(path)
        try:
            resolved = p.resolve(strict=False)
        except (OSError, RuntimeError):
            resolved = p
        exists = resolved.exists() and resolved.is_dir()
        is_git = exists and (resolved / ".git").exists()
        result = {"exists": exists, "is_git_repo": is_git, "error": None}
    except Exception as e:  # noqa: BLE001 — final safety net
        result = {"exists": False, "is_git_repo": False, "error": str(e)}
    with _lock:
        _cache[path] = (now, result)
    return result


def invalidate(*paths: str | None) -> None:
    """Drop cache entries. Call with no args to clear the whole cache."""
    with _lock:
        if not paths:
            _cache.clear()
            return
        for p in paths:
            if p:
                _cache.pop(p, None)


def derive_validity(project_dir: str | None) -> bool | None:
    """Convenience: turn a squad's project_dir into True/False/None.

    * ``None`` — project_dir unset (squad is discussion-only)
    * ``True`` — path exists AND is a git repo
    * ``False`` — configured but the fs check failed (path missing or not a git repo)
    """
    if not project_dir:
        return None
    v = validate(project_dir)
    return bool(v.get("exists") and v.get("is_git_repo"))
