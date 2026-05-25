"""Image uploads for OpenForge post composer (paste-image feature).

Tiny self-contained module — no Pillow / multipart deps. Posts ship
base64-encoded image bytes through JSON; we content-sniff against a small
MIME allowlist, dedupe by sha256, and serve back as static files.

Storage layout:
    ~/.openclaw/openforge/uploads/<sha256>.<ext>

Public API:
    save_upload(content_bytes, declared_mime) -> dict
    get_upload_path(filename) -> Path | None
    UPLOADS_DIR  -- exposed for tests
    UploadError  -- raised for any client-fixable problem
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# ─── config ──────────────────────────────────────────────────────────

UPLOADS_DIR = Path.home() / ".openclaw" / "openforge" / "uploads"
MAX_BYTES = 10 * 1024 * 1024  # 10 MB

# v0.10: per-operator workspace upload dir (for ref-based thread-create paste).
# Files are written here (not into the openforge git repo) and then registered
# via forge_refs.register(), so multi-agent ref resolution works.
WORKSPACE_BASE = Path.home() / ".openclaw"


def workspace_uploads_dir(operator: str) -> Path:
    """Return ``~/.openclaw/workspace-<operator>/openforge-uploads`` (mkdir-p)."""
    op = (operator or "scott").strip() or "scott"
    # be paranoid: forbid path traversal in the operator id
    if "/" in op or "\\" in op or ".." in op:
        raise UploadError(f"invalid operator id: {op!r}")
    p = WORKSPACE_BASE / f"workspace-{op}" / "openforge-uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p

# Map: declared mime -> file extension. Also serves as MIME allowlist.
# We content-sniff magic bytes to confirm; this dict drives both checks.
_MIME_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

# Magic-byte signatures: ext -> list[(offset, bytes_prefix)]
# Used to verify the actual file contents match the declared MIME, so a
# client can't lie about content_type and smuggle a script.
_SIGNATURES: dict[str, list[tuple[int, bytes]]] = {
    "png": [(0, b"\x89PNG\r\n\x1a\n")],
    "jpg": [(0, b"\xff\xd8\xff")],
    "gif": [(0, b"GIF87a"), (0, b"GIF89a")],
    "webp": [(0, b"RIFF"), (8, b"WEBP")],  # both must match
}

# Reverse-map ext -> canonical content-type used when serving back.
_EXT_TO_MIME: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


class UploadError(Exception):
    """User-fixable upload problem (bad mime, too large, corrupt bytes)."""


# ─── helpers ─────────────────────────────────────────────────────────

def _ensure_dir() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_mime(mime: str | None) -> str:
    if not mime:
        raise UploadError("content_type is required")
    m = mime.strip().lower().split(";")[0].strip()
    if m not in _MIME_EXT:
        raise UploadError(
            f"unsupported content_type {m!r}; allowed: "
            + ", ".join(sorted(_MIME_EXT))
        )
    return m


def _verify_magic(data: bytes, ext: str) -> None:
    sigs = _SIGNATURES.get(ext, [])
    if not sigs:
        return
    if ext == "webp":
        # webp: RIFF at 0 AND WEBP at 8 (both required)
        if not (data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP"):
            raise UploadError("bytes do not match declared webp content_type")
        return
    for offset, prefix in sigs:
        if data[offset:offset + len(prefix)] == prefix:
            return
    raise UploadError(f"bytes do not match declared {ext!r} content_type")


def _safe_ext_from_filename(name: str) -> str | None:
    """Return lowercase ext (no dot) only if it's in our allowlist; else None."""
    base = name.rsplit("/", 1)[-1]
    if "." not in base:
        return None
    ext = base.rsplit(".", 1)[-1].lower()
    return ext if ext in _SIGNATURES else None


# ─── public api ──────────────────────────────────────────────────────

def save_upload(content: bytes, mime: str | None) -> dict:
    """Persist image bytes, return metadata dict.

    Raises UploadError on validation failures.
    """
    if not isinstance(content, (bytes, bytearray)):
        raise UploadError("content must be bytes")
    if len(content) == 0:
        raise UploadError("content is empty")
    if len(content) > MAX_BYTES:
        raise UploadError(
            f"file too large: {len(content)} bytes (max {MAX_BYTES})"
        )

    m = _normalize_mime(mime)
    ext = _MIME_EXT[m]
    _verify_magic(bytes(content[:32]), ext)

    digest = hashlib.sha256(content).hexdigest()
    filename = f"{digest}.{ext}"

    _ensure_dir()
    path = UPLOADS_DIR / filename
    if not path.exists():
        # write atomically: tmp + rename, so partial reads can't race the GET
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(content)
        tmp.replace(path)

    return {
        "filename": filename,
        "url": f"/api/uploads/{filename}",
        "size": len(content),
        "content_type": _EXT_TO_MIME[ext],
        "sha256": digest,
    }


def save_upload_to_workspace(content: bytes, mime: str | None,
                              operator: str) -> dict:
    """Variant of save_upload that lands the file in the operator's workspace.

    Layout: ``~/.openclaw/workspace-<operator>/openforge-uploads/<ts>-<sha8>.<ext>``

    Returns a dict with ``abs_path``, ``filename``, ``size``, ``content_type``,
    ``sha256``. Caller is responsible for forge_refs.register() if a ref is
    desired.
    """
    if not isinstance(content, (bytes, bytearray)):
        raise UploadError("content must be bytes")
    if len(content) == 0:
        raise UploadError("content is empty")
    if len(content) > MAX_BYTES:
        raise UploadError(
            f"file too large: {len(content)} bytes (max {MAX_BYTES})"
        )
    m = _normalize_mime(mime)
    ext = _MIME_EXT[m]
    _verify_magic(bytes(content[:32]), ext)

    digest = hashlib.sha256(content).hexdigest()
    import time as _time
    ts = int(_time.time() * 1000)
    filename = f"{ts}-{digest[:12]}.{ext}"
    target_dir = workspace_uploads_dir(operator)
    path = target_dir / filename
    if not path.exists():
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(content)
        tmp.replace(path)
    return {
        "abs_path": str(path),
        "filename": filename,
        "size": len(content),
        "content_type": _EXT_TO_MIME[ext],
        "sha256": digest,
    }


def get_upload_path(filename: str) -> tuple[Path, str] | None:
    """Resolve a stored upload by filename.

    Returns (path, content_type) on hit, None on miss / bad filename.
    Strictly validates filename to prevent path traversal.
    """
    # Strict: 64 hex chars + . + allowed ext, nothing else.
    if not filename or "/" in filename or ".." in filename:
        return None
    if "." not in filename:
        return None
    stem, _, ext = filename.rpartition(".")
    ext = ext.lower()
    if ext not in _EXT_TO_MIME:
        return None
    if len(stem) != 64 or not all(c in "0123456789abcdef" for c in stem):
        return None
    path = UPLOADS_DIR / filename
    if not path.exists() or not path.is_file():
        return None
    return path, _EXT_TO_MIME[ext]
