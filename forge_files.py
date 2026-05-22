"""Filesystem-backed CRUD for OpenForge markdown files (v0.6).

Storage:
    ~/.openclaw/openforge/files/<name>.md  (flat; no subdirs)

Filename policy (strict, prevents path traversal):
    re.fullmatch(r'[A-Za-z0-9_-]+\\.md', name)

All public functions raise:
    FileNameError  — filename invalid
    NotFoundError  — file does not exist (read/update)
    AlreadyExistsError — file already exists (create)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

FILES_SUBDIR = "files"
NAME_RE = re.compile(r"[A-Za-z0-9_-]+\.md")
MAX_CONTENT_BYTES = 1_000_000  # 1 MB — generous for markdown


class FileNameError(ValueError):
    pass


class NotFoundError(LookupError):
    pass


class AlreadyExistsError(ValueError):
    pass


@dataclass
class FileMeta:
    name: str
    size: int
    mtime: float

    def as_dict(self) -> dict:
        return {"name": self.name, "size": self.size, "mtime": self.mtime}


def files_root() -> Path:
    """Resolve the storage root *every call* so tests using monkeypatched HOME work."""
    root = Path.home() / ".openclaw" / "openforge" / FILES_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise FileNameError("invalid filename")
    return name


def _meta(path: Path) -> FileMeta:
    st = path.stat()
    return FileMeta(name=path.name, size=st.st_size, mtime=st.st_mtime)


def list_files() -> list[dict]:
    root = files_root()
    out: list[dict] = []
    for child in sorted(root.iterdir()):
        if child.is_file() and NAME_RE.fullmatch(child.name):
            out.append(_meta(child).as_dict())
    return out


def read_file(name: str) -> dict:
    name = _validate_name(name)
    path = files_root() / name
    if not path.exists() or not path.is_file():
        raise NotFoundError(name)
    meta = _meta(path)
    return {
        "name": meta.name,
        "size": meta.size,
        "mtime": meta.mtime,
        "content": path.read_text(encoding="utf-8"),
    }


def create_file(name: str, content: str = "") -> dict:
    name = _validate_name(name)
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise FileNameError("content must be string")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise FileNameError("content too large")
    path = files_root() / name
    if path.exists():
        raise AlreadyExistsError(name)
    path.write_text(content, encoding="utf-8")
    return _meta(path).as_dict()


def update_file(name: str, content: str) -> dict:
    name = _validate_name(name)
    if not isinstance(content, str):
        raise FileNameError("content must be string")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise FileNameError("content too large")
    path = files_root() / name
    if not path.exists() or not path.is_file():
        raise NotFoundError(name)
    path.write_text(content, encoding="utf-8")
    return _meta(path).as_dict()
