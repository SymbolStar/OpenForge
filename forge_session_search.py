"""OpenForge v0.9.2 — Self Session Search.

Lets an agent (typically a forge-spawned child) search the *full history* of
its own main-session jsonl files, not just the slice that v0.9's context
bundle injected. Plain substring matching, case-insensitive, time-bounded.

Storage assumption (matches OpenClaw on-disk layout):
    ~/.openclaw/agents/<agent_id>/sessions/<session_id>.jsonl

Each jsonl line is a runtime event. We only look at lines with
`type == "message"` and read text out of `message.content` (string OR list of
parts with `text` fields).

PRD: docs/PRD-v0.9.2-self-session-search.md.

Public API:
    search(agent_id, query, *, days=30, max_hits=10, scope="main", now=None)
      -> dict with keys: agent, query, scope, days_window, searched_sessions,
                         total_hits, hits, truncated, warnings

`scope`:
    main  — default; skips files whose stem starts with the OpenForge prefixes
            (forge-, standup-, huddle-). I.e. real "main" history only.
    forge — only forge-spawned child sessions.
    all   — everything.

Safety knobs (PRD §4.3):
    - query length ≤ MAX_QUERY_LEN (200) chars
    - per-file size cap MAX_SESSION_BYTES (50 MB)  → over-cap files skipped
    - total session cap MAX_SESSIONS (100)         → newest-first
    - wall-clock cap TOTAL_TIMEOUT_S (3.0 s)       → partial results allowed
    - snippet truncated to SNIPPET_MAX (400) chars
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import forge_paths

AGENTS_ROOT = forge_paths.openclaw_agents_root()

# Match agent_runtime.py — these prefixes denote "forge-spawned" sessions.
_FORGE_PREFIXES = ("forge-", "standup-", "huddle-")

# Safety limits.
MAX_QUERY_LEN = 200
MAX_SESSION_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_SESSIONS = 100
TOTAL_TIMEOUT_S = 3.0
SNIPPET_MAX = 400
SNIPPET_PAD = 200  # chars of context on each side of a hit
DEFAULT_DAYS = 30
DEFAULT_MAX_HITS = 10
ABS_MAX_HITS = 50

# Match jsonl `timestamp` like "2026-05-20T01:58:22.047Z".
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


class SessionSearchError(ValueError):
    """Raised on caller-input validation errors (bad query etc.)."""


def _sessions_dir(agent_id: str) -> Path:
    # Recompute lazily so HOME monkeypatch / OPENCLAW_HOME (if added) works.
    return forge_paths.openclaw_agents_root() / agent_id / "sessions"


def _is_forge_stem(stem: str) -> bool:
    return any(stem.startswith(p) for p in _FORGE_PREFIXES)


def _classify(stem: str) -> str:
    return "forge" if _is_forge_stem(stem) else "main"


def _parse_iso(ts: str) -> float | None:
    """ISO-8601 → epoch seconds. Returns None on failure."""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        # Python 3.11+ tolerates fractional seconds + tz offset.
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _extract_text(content: Any) -> str:
    """Pull plain text out of an OpenClaw `message.content` value.

    Accepts:
      - a plain string
      - a list of parts, each {type: "text"|"output_text"|..., text: "..."}
      - a list of parts containing other shapes (toolCall etc.) → joined via best-effort
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if isinstance(part, str):
            chunks.append(part)
            continue
        if not isinstance(part, dict):
            continue
        # Common shapes.
        if "text" in part and isinstance(part["text"], str):
            chunks.append(part["text"])
            continue
        if part.get("type") == "toolCall":
            # Include tool name + stringified arguments so search can hit
            # tool invocations (e.g. exec commands referencing names).
            args = part.get("arguments")
            try:
                args_s = json.dumps(args, ensure_ascii=False) if args else ""
            except (TypeError, ValueError):
                args_s = str(args)
            chunks.append(f"[tool:{part.get('name', '?')}] {args_s}")
            continue
        if part.get("type") == "toolResult":
            r = part.get("result")
            if isinstance(r, str):
                chunks.append(r)
            elif r is not None:
                try:
                    chunks.append(json.dumps(r, ensure_ascii=False))
                except (TypeError, ValueError):
                    chunks.append(str(r))
            continue
        # Fallback: stringify dict.
        try:
            chunks.append(json.dumps(part, ensure_ascii=False))
        except (TypeError, ValueError):
            chunks.append(str(part))
    return "\n".join(c for c in chunks if c)


def _make_snippet(text: str, q_lower: str) -> tuple[str, int]:
    """Return (snippet, char_offset) centered on the first case-insensitive hit."""
    lower = text.lower()
    idx = lower.find(q_lower)
    if idx < 0:
        return "", -1
    start = max(0, idx - SNIPPET_PAD)
    end = min(len(text), idx + len(q_lower) + SNIPPET_PAD)
    snippet = text[start:end]
    # Trim to SNIPPET_MAX.
    if len(snippet) > SNIPPET_MAX:
        snippet = snippet[:SNIPPET_MAX] + "…"
    # Collapse whitespace for readable single-line preview.
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return snippet, idx


@dataclass
class _SessionMeta:
    path: Path
    stem: str
    kind: str  # "main" | "forge"
    mtime: float


def _list_sessions(agent_id: str, scope: str) -> list[_SessionMeta]:
    """List candidate .jsonl session files, newest mtime first, respecting scope.

    Excludes trajectory files (*.trajectory.jsonl) — those are runtime traces,
    not the message log.
    """
    d = _sessions_dir(agent_id)
    if not d.exists():
        return []
    out: list[_SessionMeta] = []
    for f in d.glob("*.jsonl"):
        name = f.name
        if name.endswith(".trajectory.jsonl"):
            continue
        if ".bak-" in name or ".reset." in name:
            # Skip backup/reset artifacts; they're frozen old copies.
            continue
        stem = name[: -len(".jsonl")]
        kind = _classify(stem)
        if scope == "main" and kind != "main":
            continue
        if scope == "forge" and kind != "forge":
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        out.append(_SessionMeta(path=f, stem=stem, kind=kind, mtime=mtime))
    out.sort(key=lambda m: m.mtime, reverse=True)
    return out


def _scan_session(
    meta: _SessionMeta,
    q_lower: str,
    deadline: float,
    cutoff_epoch: float | None,
) -> tuple[list[dict], str | None]:
    """Scan one jsonl file. Returns (hits_for_this_session, warning_or_None).

    Stops mid-file if `time.monotonic() >= deadline`.
    """
    hits: list[dict] = []
    try:
        size = meta.path.stat().st_size
    except OSError as e:
        return hits, f"{meta.stem}: stat failed ({e.__class__.__name__})"
    if size > MAX_SESSION_BYTES:
        return hits, f"{meta.stem}: skipped ({size} bytes > {MAX_SESSION_BYTES})"

    try:
        fh = meta.path.open("r", encoding="utf-8", errors="replace")
    except OSError as e:
        return hits, f"{meta.stem}: open failed ({e.__class__.__name__})"

    try:
        for raw_line in fh:
            if time.monotonic() >= deadline:
                return hits, f"{meta.stem}: scan aborted (timeout)"
            line = raw_line.rstrip("\n")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") != "message":
                continue
            ts_raw = obj.get("timestamp") or ""
            ts_epoch = _parse_iso(ts_raw)
            if cutoff_epoch is not None and ts_epoch is not None and ts_epoch < cutoff_epoch:
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            text = _extract_text(msg.get("content"))
            if not text:
                continue
            if q_lower not in text.lower():
                continue
            snippet, offset = _make_snippet(text, q_lower)
            hits.append({
                "session_id": meta.stem,
                "session_kind": meta.kind,
                "ts": ts_raw,
                "ts_epoch": ts_epoch,
                "role": msg.get("role") or "unknown",
                "snippet": snippet,
                "char_offset": offset,
            })
    finally:
        fh.close()
    return hits, None


def search(
    agent_id: str,
    query: str,
    *,
    days: int = DEFAULT_DAYS,
    max_hits: int = DEFAULT_MAX_HITS,
    scope: str = "main",
    now: float | None = None,
) -> dict:
    """Search an agent's session jsonls. See module docstring for behavior."""
    # ── input validation ────────────────────────────────────────────
    if not agent_id or not isinstance(agent_id, str):
        raise SessionSearchError("agent_id required")
    if not isinstance(query, str):
        raise SessionSearchError("query must be a string")
    q = query.strip()
    if not q:
        raise SessionSearchError("query is empty")
    if len(q) > MAX_QUERY_LEN:
        raise SessionSearchError(f"query too long (>{MAX_QUERY_LEN} chars)")

    if not isinstance(days, int) or days < 0:
        raise SessionSearchError("days must be a non-negative int")
    if not isinstance(max_hits, int) or max_hits < 1:
        raise SessionSearchError("max_hits must be ≥ 1")
    max_hits = min(max_hits, ABS_MAX_HITS)

    if scope not in ("main", "forge", "all"):
        raise SessionSearchError("scope must be one of: main, forge, all")

    now_t = now if now is not None else time.time()
    cutoff_epoch: float | None
    if days == 0:
        cutoff_epoch = None
    else:
        cutoff_epoch = now_t - days * 86400.0

    q_lower = q.lower()

    # ── enumerate sessions ──────────────────────────────────────────
    sessions = _list_sessions(agent_id, scope)
    # Mtime pre-filter: a session whose newest write is older than cutoff
    # can't have in-window messages.
    if cutoff_epoch is not None:
        sessions = [s for s in sessions if s.mtime >= cutoff_epoch - 3600]
    # Newest-first; cap at MAX_SESSIONS.
    truncated_sessions = len(sessions) > MAX_SESSIONS
    sessions = sessions[:MAX_SESSIONS]

    # ── scan ────────────────────────────────────────────────────────
    deadline = time.monotonic() + TOTAL_TIMEOUT_S
    all_hits: list[dict] = []
    warnings: list[str] = []
    timed_out = False

    for meta in sessions:
        if time.monotonic() >= deadline:
            timed_out = True
            warnings.append("global timeout reached before all sessions scanned")
            break
        hits, warn = _scan_session(meta, q_lower, deadline, cutoff_epoch)
        if warn:
            warnings.append(warn)
        all_hits.extend(hits)

    # ── sort + cap ──────────────────────────────────────────────────
    def _sort_key(h: dict) -> float:
        return h.get("ts_epoch") or 0.0
    all_hits.sort(key=_sort_key, reverse=True)
    total_hits = len(all_hits)
    capped = all_hits[:max_hits]
    # Drop the internal sorting helper field before returning.
    for h in capped:
        h.pop("ts_epoch", None)

    if truncated_sessions:
        warnings.append(f"only the {MAX_SESSIONS} most-recent sessions were considered")

    return {
        "agent": agent_id,
        "query": q,
        "scope": scope,
        "days_window": days,
        "searched_sessions": len(sessions),
        "total_hits": total_hits,
        "returned_hits": len(capped),
        "hits": capped,
        "truncated": total_hits > len(capped),
        "timed_out": timed_out,
        "warnings": warnings,
    }
