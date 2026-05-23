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

# Hard-coded default matches the V1.0.0 PRD §4.3 reference. Loopback
# port 18789 is the OpenClaw web-chat dev default; override with
# OPENFORGE_WEBCHAT_URL for any non-default deployment.
DEFAULT_WEBCHAT_BASE_URL = "http://127.0.0.1:18789"


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
    }
