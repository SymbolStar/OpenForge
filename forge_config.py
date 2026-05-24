"""forge_config.py — runtime config for OpenForge.

Thin module that resolves config from environment variables (with safe
defaults) and exposes a single `get_config()` for the HTTP layer to
serve via `/api/config`.

V1.0.0 surface (PRD §4.3):
  * `webchat_base_url` — where the agent web-chat UI lives. The front-end
    uses it to turn employee avatars into deep-links to
    `<webchat_base_url>/chat?session=agent:<id>:main`.

Why env-driven, not file-driven:
  * Local-cockpit ethos — no config file to forget about.
  * Tailscale / remote-host scenarios just set OPENFORGE_WEBCHAT_URL.
  * Tests can `monkeypatch.setenv` and spin a fresh server subprocess.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Hard-coded default matches the V1.0.0 PRD §4.3 reference. Loopback
# port 18789 is the OpenClaw web-chat dev default; override with
# OPENFORGE_WEBCHAT_URL for any non-default deployment.
DEFAULT_WEBCHAT_BASE_URL = "http://127.0.0.1:18789"

# Baseline version marker. `git describe` is preferred at runtime; this
# is the fallback when we're running outside a git checkout (release
# tarball, container image without .git, etc.).
FALLBACK_VERSION = "v1.0.0"

_REPO_ROOT = Path(__file__).resolve().parent
_VERSION_CACHE: str | None = None


def get_version() -> str:
    """Best-effort version string for the settings modal / `/api/config`.

    Order: `git describe --tags --always --dirty` from the repo root,
    falling back to FALLBACK_VERSION. Cached at module level — the value
    only changes across a process restart anyway.
    """
    global _VERSION_CACHE
    if _VERSION_CACHE is not None:
        return _VERSION_CACHE
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "describe",
             "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        v = (out.stdout or "").strip()
        _VERSION_CACHE = v or FALLBACK_VERSION
    except Exception:
        _VERSION_CACHE = FALLBACK_VERSION
    return _VERSION_CACHE


def get_webchat_base_url() -> str:
    """Resolve the webchat base URL, stripping a trailing slash so the
    front-end can safely append `/chat?session=...`."""
    raw = os.environ.get("OPENFORGE_WEBCHAT_URL", "").strip()
    if not raw:
        return DEFAULT_WEBCHAT_BASE_URL
    return raw.rstrip("/")


def get_config() -> dict:
    """Returned verbatim from `GET /api/config`. Keep additions
    additive — front-end caches this once at boot."""
    return {
        "webchat_base_url": get_webchat_base_url(),
        "version": get_version(),
    }
