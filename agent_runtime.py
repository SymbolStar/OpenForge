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
import signal
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

# How long to wait after SIGTERM before escalating to SIGKILL on the process
# group. Short on purpose: by the time we reach here, the agent has already
# been hung past AGENT_TIMEOUT and we just want it gone.
_GROUP_KILL_GRACE_SECONDS = float(os.environ.get("OPENFORGE_GROUP_KILL_GRACE", "5"))


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


def _killpg_safe(pgid: int, sig: int) -> None:
    """Send `sig` to process group `pgid`, swallowing already-gone errors."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def call_agent(agent_id: str, session_id: str, prompt: str, extra_env: dict[str, str] | None = None) -> str:
    """Invoke `openclaw agent --local --json`. Raises AgentError on failure.

    --local keeps the run fully sandboxed in a subprocess so
    `agent:<id>:main` is NEVER mutated. --json gives us a structured result
    on stdout (older builds wrote to stderr; ≥2026.5.5 is required).

    Subprocess runs in its OWN process group (start_new_session=True) so
    that on timeout we can SIGTERM→SIGKILL the entire descendant tree.
    Without this, an agent that backgrounds a long-lived process via its
    exec tool (e.g. `forge dev`, an MCP server, a file watcher) leaks
    orphan grandchildren that survive subprocess.run's timeout-kill of
    the direct child only. Those orphans inherit the openclaw-agent's
    stdout/stderr pipes, so communicate() never gets EOF and hangs in the
    read loop indefinitely — pinning the router's in-flight slot and
    silently dropping every subsequent @mention to that agent in the
    thread. Real incident: 2026-05-26, judy hung 11 min on `forge dev`.

    PR-B2: extra_env (default None) is merged on top of os.environ for the
    subprocess. Used by the post router to inject OPENFORGE_PROJECT_DIR
    when the squad has a valid project_dir configured, so the worktree
    helper script (PR-C1) can locate the target repo without the agent
    needing to know the path. Keys with None values are dropped.
    """
    argv = [
        OPENCLAW_BIN, "agent",
        "--local", "--json",
        "--agent", agent_id,
        "--session-id", session_id,
        "--timeout", str(AGENT_TIMEOUT),
        "--message", prompt,
    ]
    try:
        spawn_env = None
        if extra_env:
            cleaned = {k: v for k, v in extra_env.items() if v is not None}
            if cleaned:
                spawn_env = {**os.environ, **cleaned}
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=spawn_env,
            start_new_session=True,  # new process group so killpg() reaches grandchildren
        )
    except FileNotFoundError:
        raise AgentError(f"openclaw binary not found: {OPENCLAW_BIN}") from None

    pgid = proc.pid  # equals process group id thanks to start_new_session
    try:
        stdout, stderr = proc.communicate(timeout=AGENT_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        _killpg_safe(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=_GROUP_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _killpg_safe(pgid, signal.SIGKILL)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass  # truly stuck — best-effort, we already SIGKILLed the group
        # Best-effort drain to release pipe fds; we don't use the data.
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        raise AgentError(
            f"timeout after {AGENT_TIMEOUT}s (process group killed)"
        ) from None

    if proc.returncode != 0:
        tail = (stderr or stdout or "").strip().splitlines()[-3:]
        raise AgentError(
            f"openclaw agent exited {proc.returncode}: " + " | ".join(tail)
        )

    raw = (stdout or "").strip() or (stderr or "").strip()
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
