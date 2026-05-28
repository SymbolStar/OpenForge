"""OpenForge v0.8 — File References (distributed file registry).

v0.7 expected files to live inside one of the configured `fileRoots`. That
forced users to manually map every agent workspace into OpenForge, which
doesn't scale (each agent writes inside its own ~/.openclaw/workspace-<id>/).

v0.8 inverts the model: agents write files wherever they want, then POST a
*reference* into OpenForge. Files view becomes a registry of refs, not a
filesystem browser.

Storage: append-only JSONL at ~/.openclaw/openforge/refs.jsonl
    {"op":"register","ref":{...}}
    {"op":"unregister","id":"ref_..."}

Public API:
    register(label, abs_path, source_agent, ...) -> dict
    list_refs(agent=..., thread=..., squad=...) -> list[dict]
    get_ref(ref_id) -> dict | None
    read_content(ref_id) -> tuple[bytes, content_type, ref]
    write_content(ref_id, bytes_data) -> dict
    unregister(ref_id) -> bool

Raises (typed):
    RefValidationError  — bad payload
    RefNotFoundError    — id not in registry / unregistered
    RefMissingError     — registry entry exists but file gone
    RefTooLargeError    — file > 10 MB
    RefBlockedError     — MIME type not in whitelist (or symlink)
    RefReadOnlyError    — writable=False on PUT
"""
from __future__ import annotations

import difflib
import hashlib
import json
import mimetypes
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024  # 10 MB
ID_PREFIX = "ref_"

# MIME whitelist — keep it tight. Anything not listed → 403.
ALLOWED_MIME_PREFIXES = ("text/", "image/")
ALLOWED_MIME_EXACT = {
    "application/json",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
    "application/xml",
}

# ─── exceptions ─────────────────────────────────────────────────────


class RefValidationError(ValueError):
    pass


class RefNotFoundError(LookupError):
    pass


class RefMissingError(LookupError):
    """Registry entry exists, but the underlying file is gone / unreadable."""
    pass


class RefTooLargeError(ValueError):
    pass


class RefBlockedError(PermissionError):
    pass


class RefConflictError(PermissionError):
    """PUT If-Match mismatch (etag changed underneath us)."""

    def __init__(self, current_etag: str, current_content: str):
        super().__init__("etag mismatch")
        self.current_etag = current_etag
        self.current_content = current_content


class RefReadOnlyError(PermissionError):
    pass


# ─── data class ─────────────────────────────────────────────────────


@dataclass
class Ref:
    id: str
    label: str
    abs_path: str
    source_agent: str
    registered_at: float
    content_type: str = ""
    thread_id: str | None = None
    squad_id: str | None = None
    writable: bool = False
    size_hint: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "abs_path": self.abs_path,
            "source_agent": self.source_agent,
            "registered_at": self.registered_at,
            "content_type": self.content_type,
            "thread_id": self.thread_id,
            "squad_id": self.squad_id,
            "writable": self.writable,
            "size_hint": self.size_hint,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Ref:
        return cls(
            id=d["id"],
            label=d["label"],
            abs_path=d["abs_path"],
            source_agent=d["source_agent"],
            registered_at=float(d.get("registered_at") or 0),
            content_type=d.get("content_type") or "",
            thread_id=d.get("thread_id"),
            squad_id=d.get("squad_id"),
            writable=bool(d.get("writable", False)),
            size_hint=int(d.get("size_hint") or 0),
        )


# ─── state ──────────────────────────────────────────────────────────


_lock = threading.Lock()
_loaded_for: Path | None = None
_active: dict[str, Ref] = {}


def forge_dir() -> Path:
    p = Path.home() / ".openclaw" / "openforge"
    p.mkdir(parents=True, exist_ok=True)
    return p


def refs_path() -> Path:
    return forge_dir() / "refs.jsonl"


def _replay() -> None:
    """Load refs.jsonl into _active. Lock must be held."""
    global _loaded_for, _active
    p = refs_path()
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
                if op == "register":
                    r = rec.get("ref") or {}
                    if not isinstance(r, dict) or not r.get("id"):
                        continue
                    try:
                        _active[r["id"]] = Ref.from_dict(r)
                    except Exception:
                        continue
                elif op == "unregister":
                    rid = rec.get("id")
                    if rid:
                        _active.pop(rid, None)
    except OSError:
        return


def _ensure_loaded() -> None:
    """Re-replay when home dir changed (test fixtures monkeypatch HOME)."""
    global _loaded_for
    cur = refs_path()
    if _loaded_for != cur:
        _replay()


def _append(record: dict) -> None:
    p = refs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _gen_id() -> str:
    for _ in range(8):
        sid = ID_PREFIX + secrets.token_hex(3)
        if sid not in _active:
            return sid
    # ridiculously unlikely; fall back to longer
    return ID_PREFIX + hashlib.sha1(os.urandom(16)).hexdigest()[:10]


# ─── validation helpers ─────────────────────────────────────────────


def _validate_label(label) -> str:
    if not isinstance(label, str) or not label.strip():
        raise RefValidationError("label must be non-empty string")
    label = label.strip()
    if len(label) > 256:
        raise RefValidationError("label too long")
    if "/" in label or "\x00" in label:
        raise RefValidationError("label cannot contain '/' or null")
    return label


def _validate_agent(agent) -> str:
    if not isinstance(agent, str) or not agent.strip():
        raise RefValidationError("source_agent required")
    a = agent.strip()
    if len(a) > 64 or not all(c.isalnum() or c in "-_." for c in a):
        raise RefValidationError("invalid source_agent")
    return a


def _validate_optional_str(value, field_name: str, maxlen: int = 128) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RefValidationError(f"{field_name} must be string or null")
    v = value.strip()
    if not v:
        return None
    if len(v) > maxlen:
        raise RefValidationError(f"{field_name} too long")
    return v


def _resolve_abs_path(raw_path) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise RefValidationError("abs_path required")
    if not os.path.isabs(raw_path):
        raise RefValidationError("abs_path must be absolute")
    p = Path(raw_path)
    # Reject symlinks at the leaf (don't auto-follow into unexpected places).
    try:
        is_link = p.is_symlink()
    except OSError as e:
        raise RefValidationError(f"abs_path stat failed: {e}") from None
    if is_link:
        raise RefBlockedError("symlinks not allowed")
    try:
        resolved = p.resolve(strict=True)
    except FileNotFoundError:
        raise RefValidationError("file does not exist") from None
    except OSError as e:
        raise RefValidationError(f"abs_path resolve failed: {e}") from None
    if not resolved.is_file():
        raise RefValidationError("abs_path is not a regular file")
    return resolved


def _detect_mime(path: Path, override: str | None) -> str:
    if override:
        return override
    guess, _ = mimetypes.guess_type(str(path))
    if guess:
        return guess
    return "application/octet-stream"


def _mime_allowed(mime: str) -> bool:
    if not mime:
        return False
    mime = mime.split(";", 1)[0].strip().lower()
    if mime in ALLOWED_MIME_EXACT:
        return True
    for pref in ALLOWED_MIME_PREFIXES:
        if mime.startswith(pref):
            return True
    return False


# ─── public API ─────────────────────────────────────────────────────


def register(
    *,
    label,
    abs_path,
    source_agent,
    thread_id=None,
    squad_id=None,
    writable: bool = False,
    content_type: str | None = None,
) -> dict:
    """Register a new file reference. Idempotent on (source_agent, resolved_abs_path).

    Returns the ref as a dict.
    """
    label_clean = _validate_label(label)
    agent_clean = _validate_agent(source_agent)
    thread_clean = _validate_optional_str(thread_id, "thread_id")
    squad_clean = _validate_optional_str(squad_id, "squad_id")
    resolved = _resolve_abs_path(abs_path)
    try:
        st = resolved.stat()
    except OSError as e:
        raise RefValidationError(f"abs_path stat failed: {e}") from None
    if st.st_size > MAX_BYTES:
        raise RefTooLargeError(f"file too large: {st.st_size} > {MAX_BYTES}")
    mime = _detect_mime(resolved, content_type)
    abs_str = str(resolved)

    with _lock:
        _ensure_loaded()
        # Idempotency: same source_agent + resolved abs_path → return existing.
        for existing in _active.values():
            if existing.source_agent == agent_clean and existing.abs_path == abs_str:
                return existing.to_dict()
        rid = _gen_id()
        ref = Ref(
            id=rid,
            label=label_clean,
            abs_path=abs_str,
            source_agent=agent_clean,
            registered_at=time.time(),
            content_type=mime,
            thread_id=thread_clean,
            squad_id=squad_clean,
            writable=bool(writable),
            size_hint=int(st.st_size),
        )
        _append({"op": "register", "ref": ref.to_dict()})
        _active[rid] = ref
        return ref.to_dict()


def get_ref(ref_id: str) -> dict | None:
    with _lock:
        _ensure_loaded()
        r = _active.get(ref_id)
        return r.to_dict() if r else None


def list_refs(
    *,
    agent: str | None = None,
    thread: str | None = None,
    squad: str | None = None,
) -> list[dict]:
    with _lock:
        _ensure_loaded()
        out = list(_active.values())
    if agent:
        out = [r for r in out if r.source_agent == agent]
    if thread:
        out = [r for r in out if r.thread_id == thread]
    if squad:
        out = [r for r in out if r.squad_id == squad]
    out.sort(key=lambda r: r.registered_at, reverse=True)
    return [r.to_dict() for r in out]


def _compute_etag(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:16]


def read_content(ref_id: str) -> tuple[bytes, str, dict]:
    """Return (body_bytes, content_type, ref_dict).

    Raises RefNotFoundError / RefMissingError / RefTooLargeError / RefBlockedError.
    """
    with _lock:
        _ensure_loaded()
        ref = _active.get(ref_id)
    if not ref:
        raise RefNotFoundError(ref_id)
    p = Path(ref.abs_path)
    try:
        if p.is_symlink():
            raise RefBlockedError("symlinks not allowed")
        if not p.exists() or not p.is_file():
            raise RefMissingError(ref.abs_path)
        st = p.stat()
    except RefBlockedError:
        raise
    except OSError:
        raise RefMissingError(ref.abs_path) from None
    if st.st_size > MAX_BYTES:
        raise RefTooLargeError(f"file too large: {st.st_size}")
    mime = ref.content_type or _detect_mime(p, None)
    if not _mime_allowed(mime):
        raise RefBlockedError(f"mime not allowed: {mime}")
    try:
        data = p.read_bytes()
    except OSError as e:
        raise RefMissingError(str(e)) from None
    return data, mime, ref.to_dict()


def write_content(ref_id: str, body: bytes, *, if_match: str | None = None) -> dict:
    """Overwrite the referenced file.

    v1.1 behaviour: `.md` files are always editable regardless of
    `ref.writable` (per PRD: 「所有人可编辑 / 只 .md」). Non-.md still
    honours the legacy `writable` flag.

    `if_match` is an ETag string from a prior GET. If supplied and the
    file has changed underneath, raises RefConflictError carrying the
    current etag + content so the UI can offer a 3-way decision.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise RefValidationError("body must be bytes")
    if len(body) > MAX_BYTES:
        raise RefTooLargeError(f"body too large: {len(body)}")
    with _lock:
        _ensure_loaded()
        ref = _active.get(ref_id)
    if not ref:
        raise RefNotFoundError(ref_id)
    p = Path(ref.abs_path)
    is_md = p.suffix.lower() == ".md"
    if not is_md and not ref.writable:
        raise RefReadOnlyError(ref_id)
    if not is_md:
        # v1: only .md is editable through this path (PRD AC). Non-md
        # falls back to legacy writable semantics handled above; reject
        # the rest defensively.
        raise RefValidationError("only .md files are editable in v1")
    if p.is_symlink():
        raise RefBlockedError("symlinks not allowed")
    if not p.exists() or not p.is_file():
        raise RefMissingError(ref.abs_path)
    # Read current bytes to compute current etag (cheap for md).
    try:
        current = p.read_bytes()
    except OSError as e:
        raise RefMissingError(str(e)) from None
    current_etag = _compute_etag(current)
    if if_match is not None and if_match != current_etag:
        try:
            current_text = current.decode("utf-8", errors="replace")
        except Exception:
            current_text = ""
        raise RefConflictError(current_etag, current_text)
    # Atomic write: tmp → fsync → os.replace.
    body_bytes = bytes(body)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(body_bytes)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, p)
    st = p.stat()
    new_etag = _compute_etag(body_bytes)
    # Compute line-diff (+N / -M) vs prior content.
    try:
        prev_lines = current.decode("utf-8", errors="replace").splitlines()
        new_lines = body_bytes.decode("utf-8", errors="replace").splitlines()
        sm = difflib.SequenceMatcher(a=prev_lines, b=new_lines, autojunk=False)
        added = removed = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "insert":
                added += j2 - j1
            elif tag == "delete":
                removed += i2 - i1
            elif tag == "replace":
                added += j2 - j1
                removed += i2 - i1
        diff_summary = {"added": added, "removed": removed}
    except Exception:
        diff_summary = {"added": 0, "removed": 0}
    return {
        "id": ref.id,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "etag": new_etag,
        "diff": diff_summary,
        "label": ref.label,
        "source_agent": ref.source_agent,
    }


def unregister(ref_id: str) -> bool:
    with _lock:
        _ensure_loaded()
        if ref_id not in _active:
            return False
        _append({"op": "unregister", "id": ref_id})
        _active.pop(ref_id, None)
        return True


def _reset_for_tests() -> None:
    """Internal: drop the in-memory cache so the next call replays disk."""
    global _loaded_for, _active
    with _lock:
        _loaded_for = None
        _active = {}
