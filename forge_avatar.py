"""Agent avatar storage for OpenForge.

Stores a cropped avatar at ~/.openclaw/workspace-<agent>/avatar.png and keeps
the matching IDENTITY.md `- **Avatar:** <abs path>` line in sync.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AVATAR_LINE_RE = re.compile(r"^\s*-\s*\*\*Avatar:\*\*\s*.*$", re.MULTILINE)
EMOJI_LINE_RE = re.compile(r"^\s*-\s*\*\*Emoji:\*\*\s*.*$", re.MULTILINE)
MAX_BYTES = 2 * 1024 * 1024


class AvatarError(ValueError):
    pass


class UnsupportedAvatarError(AvatarError):
    pass


def fnv1a(input_value: str) -> int:
    h = 0x811C9DC5
    for b in str(input_value or "").encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def _validate_agent_id(agent_id: str) -> str:
    if not isinstance(agent_id, str) or not AGENT_ID_RE.fullmatch(agent_id):
        raise AvatarError("invalid agent_id")
    return agent_id


def workspace_dir(agent_id: str) -> Path:
    return Path.home() / ".openclaw" / f"workspace-{_validate_agent_id(agent_id)}"


def avatar_path(agent_id: str) -> Path:
    return workspace_dir(agent_id) / "avatar.png"


def identity_path(agent_id: str) -> Path:
    return workspace_dir(agent_id) / "IDENTITY.md"


def sniff_image_type(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def patch_identity_avatar(text: str, avatar_abs_path: str | None) -> str:
    lines = (text or "").splitlines()
    had_trailing_newline = (text or "").endswith("\n")
    if avatar_abs_path:
        avatar_line = f"- **Avatar:** {avatar_abs_path}"
        for idx, line in enumerate(lines):
            if AVATAR_LINE_RE.match(line):
                lines[idx] = avatar_line
                out = "\n".join(lines)
                return out + ("\n" if had_trailing_newline or lines else "")
        insert_at = None
        for idx, line in enumerate(lines):
            if EMOJI_LINE_RE.match(line):
                insert_at = idx + 1
                break
        if insert_at is None:
            lines.append(avatar_line)
        else:
            lines.insert(insert_at, avatar_line)
    else:
        lines = [line for line in lines if not AVATAR_LINE_RE.match(line)]
    out = "\n".join(lines)
    if out or had_trailing_newline:
        out += "\n"
    return out


def sync_identity_avatar(agent_id: str, avatar_abs_path: str | None) -> None:
    path = identity_path(agent_id)
    text = path.read_text(encoding="utf-8") if path.exists() else "# IDENTITY.md\n"
    _atomic_write_text(path, patch_identity_avatar(text, avatar_abs_path))


def save_avatar(agent_id: str, raw: bytes) -> dict:
    _validate_agent_id(agent_id)
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise AvatarError("avatar body required")
    if len(raw) > MAX_BYTES:
        raise AvatarError(f"avatar too large: max {MAX_BYTES} bytes")
    mime = sniff_image_type(bytes(raw))
    if mime is None:
        raise UnsupportedAvatarError("unsupported image type")
    path = avatar_path(agent_id)
    previous = path.read_bytes() if path.exists() else None
    _atomic_write_bytes(path, bytes(raw))
    try:
        sync_identity_avatar(agent_id, str(path))
    except Exception:
        if previous is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            _atomic_write_bytes(path, previous)
        raise
    return avatar_info(agent_id) | {"content_type": mime}


def delete_avatar(agent_id: str) -> dict:
    _validate_agent_id(agent_id)
    path = avatar_path(agent_id)
    if path.exists():
        path.unlink()
        _fsync_dir(path.parent)
    sync_identity_avatar(agent_id, None)
    return {"deleted": True, "agent": agent_id}


def avatar_info(agent_id: str) -> dict:
    path = avatar_path(agent_id)
    st = path.stat()
    with open(path, "rb") as f:
        content_type = sniff_image_type(f.read(12)) or "application/octet-stream"
    mtime_ms = int(st.st_mtime * 1000)
    return {
        "agent": agent_id,
        "abs_path": str(path),
        "size": st.st_size,
        "content_type": content_type,
        "mtime_ms": mtime_ms,
        "url": f"/api/agents/{agent_id}/avatar?v={mtime_ms}",
    }
