"""OpenForge Favorites — file-level favorites keyed by abs_path.

PRD v1.1 (alice 2026-05-28) implementation. Schema rationale:
- `refs` table dedupes on (source_agent, abs_path), so the same .md registered
  by two different agents produces two ref_ids. Favoriting at the ref level
  would surface duplicates. We instead key favorites on `abs_path` (PK),
  living in their own JSONL log, untying favorites from refs lifecycle.
- `first_seen_*` columns snapshot the ref/thread/agent context at the moment
  scott first favorited the file — later re-registrations don't rewrite the
  meta line on the card.
- Per-user is hard-coded out for V1 (scott is the only user). V2 will promote
  PK to (user_id, abs_path) without touching this log format.

Storage: ~/.openclaw/openforge/favorites.jsonl append-only
    {"op":"set","abs_path":"…","favorited_at":<float>,"first_seen_ref_id":…,
     "first_seen_thread_id":…,"first_seen_agent":…}
    {"op":"del","abs_path":"…"}

Public API:
    set_favorite(abs_path, *, ref_id=None, thread_id=None, source_agent=None) -> dict
    unset_favorite(abs_path) -> bool
    is_favorite(abs_path) -> bool
    get_favorite(abs_path) -> dict | None
    list_favorites() -> list[dict]        (sorted by favorited_at DESC)
    list_with_status() -> list[dict]      (adds label/preview/missing_state)
    count() -> int

Raises: FavoriteValidationError on bad abs_path.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path


class FavoriteValidationError(ValueError):
    pass


# ─── storage ────────────────────────────────────────────────────────


_lock = threading.Lock()
_loaded_for: Path | None = None
_active: dict[str, dict] = {}


def _forge_dir() -> Path:
    p = Path.home() / ".openclaw" / "openforge"
    p.mkdir(parents=True, exist_ok=True)
    return p


def favorites_path() -> Path:
    return _forge_dir() / "favorites.jsonl"


def _replay() -> None:
    global _loaded_for, _active
    p = favorites_path()
    _active = {}
    _loaded_for = p
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                op = rec.get("op")
                ap = rec.get("abs_path")
                if not ap or not isinstance(ap, str):
                    continue
                if op == "set":
                    # latest "set" wins; first_seen_* preserved from earliest
                    # write if already present.
                    prev = _active.get(ap)
                    rec_clean = {
                        "abs_path": ap,
                        "favorited_at": float(rec.get("favorited_at") or time.time()),
                        "first_seen_ref_id": prev["first_seen_ref_id"] if prev else rec.get("first_seen_ref_id"),
                        "first_seen_thread_id": prev["first_seen_thread_id"] if prev else rec.get("first_seen_thread_id"),
                        "first_seen_agent": prev["first_seen_agent"] if prev else rec.get("first_seen_agent"),
                    }
                    # If this is the very first set (no prev), take rec's first_seen.
                    if prev is None:
                        rec_clean["first_seen_ref_id"] = rec.get("first_seen_ref_id")
                        rec_clean["first_seen_thread_id"] = rec.get("first_seen_thread_id")
                        rec_clean["first_seen_agent"] = rec.get("first_seen_agent")
                    _active[ap] = rec_clean
                elif op == "del":
                    _active.pop(ap, None)
    except OSError:
        return


def _ensure_loaded() -> None:
    global _loaded_for
    cur = favorites_path()
    if _loaded_for != cur:
        _replay()


def _append(record: dict) -> None:
    p = favorites_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ─── validation ─────────────────────────────────────────────────────


def _validate_abs_path(raw) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise FavoriteValidationError("abs_path required")
    s = raw.strip()
    if not os.path.isabs(s):
        raise FavoriteValidationError("abs_path must be absolute")
    if "\x00" in s or len(s) > 4096:
        raise FavoriteValidationError("abs_path invalid")
    return s


def _opt_str(v, name: str, maxlen: int = 256) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise FavoriteValidationError(f"{name} must be string or null")
    v = v.strip()
    if not v:
        return None
    if len(v) > maxlen:
        raise FavoriteValidationError(f"{name} too long")
    return v


# ─── public API ─────────────────────────────────────────────────────


def set_favorite(abs_path, *, ref_id=None, thread_id=None, source_agent=None) -> dict:
    """Upsert a favorite. Idempotent on abs_path (PK).

    first_seen_* is written only on the very first set; subsequent toggles
    don't overwrite the snapshot.
    """
    ap = _validate_abs_path(abs_path)
    ref_clean = _opt_str(ref_id, "ref_id", 64)
    thread_clean = _opt_str(thread_id, "thread_id", 128)
    agent_clean = _opt_str(source_agent, "source_agent", 64)
    with _lock:
        _ensure_loaded()
        existing = _active.get(ap)
        if existing:
            # Already favorited → no-op, return current row.
            return dict(existing)
        rec = {
            "abs_path": ap,
            "favorited_at": time.time(),
            "first_seen_ref_id": ref_clean,
            "first_seen_thread_id": thread_clean,
            "first_seen_agent": agent_clean,
        }
        _active[ap] = rec
        _append({"op": "set", **rec})
        return dict(rec)


def unset_favorite(abs_path) -> bool:
    """Remove a favorite. Returns True if a row was removed."""
    ap = _validate_abs_path(abs_path)
    with _lock:
        _ensure_loaded()
        if ap not in _active:
            return False
        _active.pop(ap, None)
        _append({"op": "del", "abs_path": ap})
        return True


def is_favorite(abs_path) -> bool:
    try:
        ap = _validate_abs_path(abs_path)
    except FavoriteValidationError:
        return False
    with _lock:
        _ensure_loaded()
        return ap in _active


def get_favorite(abs_path) -> dict | None:
    try:
        ap = _validate_abs_path(abs_path)
    except FavoriteValidationError:
        return None
    with _lock:
        _ensure_loaded()
        v = _active.get(ap)
        return dict(v) if v else None


def list_favorites() -> list[dict]:
    with _lock:
        _ensure_loaded()
        out = [dict(v) for v in _active.values()]
    out.sort(key=lambda r: r.get("favorited_at") or 0, reverse=True)
    return out


def count() -> int:
    with _lock:
        _ensure_loaded()
        return len(_active)


# ─── enrichment: preview + missing_state ────────────────────────────


_H_RE = re.compile(r"^#{1,2}\s+(.+?)\s*$")


def _compute_preview(path: str) -> str:
    """First non-empty H1/H2 → fallback first non-empty text line → '(无预览)'.

    Reads at most ~16KB / 200 lines. Returns plain text trimmed to 80 chars.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = []
            for i, line in enumerate(fh):
                if i >= 200:
                    break
                head.append(line)
                if sum(len(x) for x in head) > 16384:
                    break
    except OSError:
        return "(无预览)"
    first_text = None
    for raw in head:
        s = raw.strip()
        if not s:
            continue
        m = _H_RE.match(s)
        if m:
            text = m.group(1).strip()
            if text:
                return text[:80]
        if first_text is None and not s.startswith(("---", "+++")):
            first_text = s
    if first_text:
        return first_text[:80]
    return "(无预览)"


async def _stat_one(path: str, timeout_s: float) -> str:
    """Return 'present' | 'missing' | 'unknown'.

    'unknown' covers timeout, EIO, EPERM and any other OSError — judy's
    sleep-disk concern: misclassifying a spun-down volume as 'missing'
    would let the UI prompt scott to delete real favorites. We refuse to
    do that without certainty.
    """
    loop = asyncio.get_event_loop()
    def _do():
        try:
            os.stat(path)
            return "present"
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unknown"
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _do), timeout=timeout_s)
    except TimeoutError:
        return "unknown"
    except Exception:
        return "unknown"


def list_with_status(*, stat_timeout_ms: int = 100) -> list[dict]:
    """Return favorites enriched with label, preview, missing_state.

    Concurrent stat with per-path timeout (PRD §7.2, AC-12). preview is read
    only for present files — missing/unknown cards don't need it and reading
    might hang anyway.
    """
    rows = list_favorites()
    if not rows:
        return []
    timeout_s = max(0.001, stat_timeout_ms / 1000.0)

    async def _runner():
        return await asyncio.gather(*[_stat_one(r["abs_path"], timeout_s) for r in rows])

    try:
        loop = asyncio.new_event_loop()
        try:
            states = loop.run_until_complete(_runner())
        finally:
            loop.close()
    except Exception:
        # If asyncio itself dies, fall back to "unknown" for every row rather
        # than crash the GET — degrades to "状态未知" cards but never wipes.
        states = ["unknown"] * len(rows)

    out = []
    for r, state in zip(rows, states, strict=False):
        ap = r["abs_path"]
        label = os.path.basename(ap) or ap
        preview = _compute_preview(ap) if state == "present" else ""
        out.append({
            "abs_path": ap,
            "label": label,
            "favorited_at": r.get("favorited_at"),
            "source_agent": r.get("first_seen_agent"),
            "source_thread_id": r.get("first_seen_thread_id"),
            "first_seen_ref_id": r.get("first_seen_ref_id"),
            "preview": preview,
            "missing_state": state,
        })
    return out


def _reset_for_tests() -> None:
    global _loaded_for, _active
    with _lock:
        _loaded_for = None
        _active = {}
