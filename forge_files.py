"""Filesystem-backed CRUD for OpenForge markdown files (v0.7 multi-root).

v0.6 stored everything under one flat dir:
    ~/.openclaw/openforge/files/<name>.md

v0.7 introduces *file roots* — multiple registered directories that can be
browsed/edited from the Files view. Roots come from
    ~/.openclaw/openforge/config.json  → key "fileRoots"
…with a default of a single writable `files` root for backward compatibility.

Filename policy (strict, prevents path traversal):
    re.fullmatch(r'[A-Za-z0-9_.\\-]+\\.md', name)  AND  no leading dot

All public helpers raise:
    FileNameError       — filename / root id / config invalid
    NotFoundError       — file (or root) not found on read/update
    AlreadyExistsError  — file already exists on create
    ReadOnlyError       — root is read-only on write attempts
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

FILES_SUBDIR = "files"
# Allow `.` so PRD-v0.7-thread-files-linking.md works.
NAME_RE = re.compile(r"[A-Za-z0-9_.\-]+\.md")
ROOT_ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,32}")
MAX_CONTENT_BYTES = 1_000_000  # 1 MB


class FileNameError(ValueError):
    pass


class NotFoundError(LookupError):
    pass


class AlreadyExistsError(ValueError):
    pass


class ReadOnlyError(PermissionError):
    pass


# ─── data classes ─────────────────────────────────────────────────────


@dataclass
class FileMeta:
    name: str
    size: int
    mtime: float

    def as_dict(self) -> dict:
        return {"name": self.name, "size": self.size, "mtime": self.mtime}


@dataclass
class Root:
    id: str
    label: str
    path: Path
    writable: bool
    globs: tuple[str, ...] = ()  # if non-empty, only show files matching any glob

    def to_summary(self, count: int) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "writable": self.writable,
            "count": count,
        }


# ─── path helpers ─────────────────────────────────────────────────────


def forge_dir() -> Path:
    """Root of the OpenForge data dir (always under $HOME, honors monkeypatch)."""
    p = Path.home() / ".openclaw" / "openforge"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _repo_root() -> Path:
    """Resolve <openforge_repo> placeholder = directory containing this module."""
    return Path(__file__).resolve().parent


def _expand(path_str: str) -> Path:
    s = path_str.replace("<openforge_repo>", str(_repo_root()))
    s = os.path.expanduser(s)
    return Path(s).resolve()


def config_path() -> Path:
    return forge_dir() / "config.json"


# ─── config loading ───────────────────────────────────────────────────


def _default_roots_config() -> list[dict]:
    return [
        {
            "id": "files",
            "label": "Files",
            "path": str(forge_dir() / FILES_SUBDIR),
            "writable": True,
        }
    ]


def _load_raw_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def load_roots() -> list[Root]:
    """Resolve every configured root. Skips entries with bad/missing paths.

    Each call re-reads disk so tests that mutate $HOME / config.json see
    fresh state.
    """
    cfg = _load_raw_config()
    raw = cfg.get("fileRoots")
    if not isinstance(raw, list) or not raw:
        raw = _default_roots_config()

    seen: set[str] = set()
    out: list[Root] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rid = entry.get("id")
        if not isinstance(rid, str) or not ROOT_ID_RE.fullmatch(rid):
            continue
        if rid in seen:
            continue
        path_s = entry.get("path")
        if not isinstance(path_s, str) or not path_s:
            continue
        try:
            path = _expand(path_s)
        except Exception:
            continue
        # Auto-create the default `files` root so first-run works.
        if rid == "files":
            try:
                path.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        if not path.exists() or not path.is_dir():
            # Skip non-existent roots silently (caller may have stale config).
            continue
        label = entry.get("label") or rid
        writable = bool(entry.get("writable", True))
        globs_raw = entry.get("globs") or ()
        if not isinstance(globs_raw, (list, tuple)):
            globs_raw = ()
        globs = tuple(g for g in globs_raw if isinstance(g, str))
        out.append(Root(id=rid, label=str(label), path=path, writable=writable, globs=globs))
        seen.add(rid)
    if not out:
        # Final fallback so callers can always rely on at least the default.
        fallback = forge_dir() / FILES_SUBDIR
        fallback.mkdir(parents=True, exist_ok=True)
        out.append(Root(id="files", label="Files", path=fallback, writable=True))
    return out


def get_root(root_id: str) -> Root | None:
    if not isinstance(root_id, str):
        return None
    for r in load_roots():
        if r.id == root_id:
            return r
    return None


def default_root() -> Root:
    return load_roots()[0]


# ─── validation ───────────────────────────────────────────────────────


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise FileNameError("invalid filename")
    if name.startswith("."):
        raise FileNameError("invalid filename")
    return name


def _safe_join(root: Root, name: str) -> Path:
    """Validate name and make sure the resolved path stays inside root.path."""
    name = _validate_name(name)
    candidate = (root.path / name).resolve()
    try:
        candidate.relative_to(root.path)
    except ValueError:
        raise FileNameError("invalid path") from None
    return candidate


def _glob_match(root: Root, filename: str) -> bool:
    if not root.globs:
        return True
    import fnmatch
    return any(fnmatch.fnmatch(filename, g) for g in root.globs)


def _meta(path: Path) -> FileMeta:
    st = path.stat()
    return FileMeta(name=path.name, size=st.st_size, mtime=st.st_mtime)


# ─── public API (root-aware) ──────────────────────────────────────────


def files_root() -> Path:
    """Back-compat shim: path of the first (default) root."""
    return default_root().path


def list_file_roots() -> list[dict]:
    out = []
    for r in load_roots():
        try:
            count = sum(
                1 for child in r.path.iterdir()
                if child.is_file()
                and NAME_RE.fullmatch(child.name)
                and not child.name.startswith(".")
                and _glob_match(r, child.name)
            )
        except OSError:
            count = 0
        out.append(r.to_summary(count))
    return out


def list_files(root_id: str | None = None) -> list[dict]:
    root = get_root(root_id) if root_id else default_root()
    if root is None:
        raise NotFoundError("unknown root")
    out: list[dict] = []
    try:
        children = sorted(root.path.iterdir())
    except OSError:
        return out
    for child in children:
        if not child.is_file():
            continue
        if not NAME_RE.fullmatch(child.name) or child.name.startswith("."):
            continue
        if not _glob_match(root, child.name):
            continue
        out.append(_meta(child).as_dict())
    return out


def read_file(name: str, root_id: str | None = None) -> dict:
    root = get_root(root_id) if root_id else default_root()
    if root is None:
        raise NotFoundError("unknown root")
    path = _safe_join(root, name)
    if not path.exists() or not path.is_file():
        raise NotFoundError(name)
    if not _glob_match(root, path.name):
        raise NotFoundError(name)
    meta = _meta(path)
    return {
        "name": meta.name,
        "size": meta.size,
        "mtime": meta.mtime,
        "content": path.read_text(encoding="utf-8"),
        "root": root.id,
        "writable": root.writable,
    }


def create_file(name: str, content: str = "", root_id: str | None = None) -> dict:
    root = get_root(root_id) if root_id else default_root()
    if root is None:
        raise NotFoundError("unknown root")
    if not root.writable:
        raise ReadOnlyError(root.id)
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise FileNameError("content must be string")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise FileNameError("content too large")
    path = _safe_join(root, name)
    if path.exists():
        raise AlreadyExistsError(name)
    path.write_text(content, encoding="utf-8")
    meta = _meta(path).as_dict()
    meta["root"] = root.id
    return meta


def update_file(name: str, content: str, root_id: str | None = None) -> dict:
    root = get_root(root_id) if root_id else default_root()
    if root is None:
        raise NotFoundError("unknown root")
    if not root.writable:
        raise ReadOnlyError(root.id)
    if not isinstance(content, str):
        raise FileNameError("content must be string")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise FileNameError("content too large")
    path = _safe_join(root, name)
    if not path.exists() or not path.is_file():
        raise NotFoundError(name)
    path.write_text(content, encoding="utf-8")
    meta = _meta(path).as_dict()
    meta["root"] = root.id
    return meta
