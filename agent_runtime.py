"""
agent_runtime.py — shared helpers for spawning `openclaw agent` from OpenForge.

Extracted from the (now-retired) run_standup.py so post_router stays slim and
the standup CLI can be removed entirely. Provides:
  - main-session snapshot/restore (works around `--session-id` hijacking the
    agent's primary chat session on builds that don't yet honour --local)
  - clean()/is_empty() output normalization
  - call_agent() that shells out to `openclaw agent --local --json`

No threads, no atexit handlers, no global state besides _resolve_openclaw_bin
caching: callers are responsible for ordering snapshot → call → restore.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

AGENTS_ROOT = Path.home() / ".openclaw" / "agents"


# ─── main-session snapshot / restore ─────────────────────────────────
# `openclaw agent --session-id X` MUTATES agent:<id>:main to point at X on
# old builds. --local on ≥2026.5.7 sidesteps this, but the snapshot/restore
# layer is kept as a defensive belt-and-suspenders so a misconfigured
# OPENFORGE_OPENCLAW_BIN can't quietly hijack the agent's primary session.

def _sessions_path(agent_id: str) -> Path:
    return AGENTS_ROOT / agent_id / "sessions" / "sessions.json"


# Session-id prefixes that mean "this main was hijacked by OpenForge".
_FORGE_SIDS = ("forge-", "standup-", "huddle-")


def _is_forge_sid(sid: str | None) -> bool:
    sid = sid or ""
    return any(sid.startswith(p) for p in _FORGE_SIDS)


def _find_clean_main(agent_id: str) -> dict | None:
    """Best-effort: pick the agent's most recently modified non-forge session."""
    sess_dir = AGENTS_ROOT / agent_id / "sessions"
    if not sess_dir.exists():
        return None
    best: tuple[float, Path] | None = None
    for f in sess_dir.glob("*.jsonl"):
        name = f.name
        if name.endswith(".trajectory.jsonl"):
            continue
        stem = name[: -len(".jsonl")]
        if _is_forge_sid(stem):
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, f)
    if best is None:
        return None
    f = best[1]
    stem = f.name[: -len(".jsonl")]
    return {
        "agent": agent_id,
        "sessionId": stem,
        "sessionFile": str(f),
        "snapshotAt": int(time.time() * 1000),
        "recoveredFromDisk": True,
    }


def snapshot_main(agent_id: str) -> dict | None:
    p = _sessions_path(agent_id)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    main = d.get(f"agent:{agent_id}:main")
    if not isinstance(main, dict):
        return _find_clean_main(agent_id)
    cur_sid = main.get("sessionId") or ""
    if _is_forge_sid(cur_sid):
        # main is already polluted from a previous crashed run.
        recovered = _find_clean_main(agent_id)
        if recovered:
            return recovered
        return None
    return {
        "agent": agent_id,
        "sessionId": main.get("sessionId"),
        "sessionFile": main.get("sessionFile"),
        "snapshotAt": int(time.time() * 1000),
    }


def restore_main(agent_id: str, snapshot: dict) -> bool:
    p = _sessions_path(agent_id)
    if not p.exists() or not snapshot or not snapshot.get("sessionId"):
        return False
    if _is_forge_sid(snapshot["sessionId"]):
        return False
    try:
        d = json.loads(p.read_text())
    except Exception:
        return False
    key = f"agent:{agent_id}:main"
    main = d.get(key)
    if not isinstance(main, dict):
        return False
    cur_sid = main.get("sessionId") or ""
    if not _is_forge_sid(cur_sid):
        return False
    main["sessionId"] = snapshot["sessionId"]
    main["sessionFile"] = snapshot["sessionFile"]
    main["updatedAt"] = int(time.time() * 1000)
    main["restoredFromOpenForge"] = True
    d[key] = main
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    os.replace(tmp, p)
    return True


# ─── output normalization ────────────────────────────────────────────
NOISE_PATTERNS = [
    re.compile(r"^\[plugins\].*$", re.MULTILINE),
    re.compile(r"^Config warnings:.*$", re.MULTILINE),
    re.compile(r"^- plugins\..*$", re.MULTILINE),
    re.compile(r"^🦞 OpenClaw.*$", re.MULTILINE),
]
EMPTY_MARKERS = {"completed", "", "_(空回复)_"}


def clean(text: str) -> str:
    out = text or ""
    for pat in NOISE_PATTERNS:
        out = pat.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def is_empty(text: str) -> bool:
    return clean(text).lower() in EMPTY_MARKERS


# ─── agent CLI bridge ─────────────────────────────────────────────────
class AgentError(RuntimeError):
    pass


AGENT_TIMEOUT = int(os.environ.get("OPENFORGE_AGENT_TIMEOUT", "1800"))  # 30 min


def _resolve_openclaw_bin() -> str:
    """Pick the right openclaw binary.

    Order:
      1. $OPENFORGE_OPENCLAW_BIN (explicit operator override)
      2. ~/.nvm/versions/node/*/bin/openclaw  (usually the newest one
         because Control UI is launched from it)
      3. plain `openclaw` from PATH

    Why pin away from PATH by default: on some hosts PATH resolves to a
    homebrew install (2026.4.22) that pre-dates the --local + --session-id
    fix; calling it pollutes agent main pointers. The nvm install ships
    ≥2026.5.7 where --local writes a real isolated session and never
    touches sessions.json.
    """
    override = os.environ.get("OPENFORGE_OPENCLAW_BIN")
    if override and Path(override).exists():
        return override
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        candidates = sorted(
            (p / "bin" / "openclaw" for p in nvm_root.iterdir() if p.is_dir()),
            key=lambda p: p.parent.parent.name, reverse=True,
        )
        for c in candidates:
            if c.exists():
                return str(c)
    return "openclaw"


OPENCLAW_BIN = _resolve_openclaw_bin()


def call_agent(agent_id: str, session_id: str, prompt: str) -> str:
    """Invoke `openclaw agent --local --json`. Raises AgentError on failure.

    --local keeps the run fully sandboxed in a subprocess so
    `agent:<id>:main` is NEVER mutated. --json gives us a structured result
    on stdout (older builds wrote to stderr; ≥2026.5.5 is required).
    """
    try:
        result = subprocess.run(
            [
                OPENCLAW_BIN, "agent",
                "--local", "--json",
                "--agent", agent_id,
                "--session-id", session_id,
                "--timeout", str(AGENT_TIMEOUT),
                "--message", prompt,
            ],
            capture_output=True, text=True, timeout=AGENT_TIMEOUT + 30,
        )
    except subprocess.TimeoutExpired:
        raise AgentError(f"timeout after {AGENT_TIMEOUT}s") from None
    except FileNotFoundError:
        raise AgentError(f"openclaw binary not found: {OPENCLAW_BIN}") from None

    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        raise AgentError(
            f"openclaw agent exited {result.returncode}: " + " | ".join(tail)
        )

    raw = (result.stdout or "").strip() or (result.stderr or "").strip()
    if not raw:
        raise AgentError("openclaw produced no output")
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        return clean(raw)
    payloads = blob.get("payloads") or []
    text = "\n\n".join(
        (p.get("text") or "").strip() for p in payloads if isinstance(p, dict)
    ).strip()
    return clean(text)
