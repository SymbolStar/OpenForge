"""v0.7 chip hit counter (PRD v1.2 follow-up, judy review proposal).

Track how often the deprecated `[[root/name.md]]` chip code path inside
`forge_files.py` is touched. Counts are persisted to:

    ~/.openclaw/openforge/v07_chip_hits.json
    {"daily": {"YYYY-MM-DD": int, ...}, "total": int, "first_seen": <iso>}

The point is to make Workspace / v0.7 chip deprecation observable so the
audit follow-up has a real "scott last used X days ago" signal instead of
gut feeling. Best-effort: I/O errors swallowed, never raises.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

_lock = threading.Lock()


def _state_path() -> Path:
    p = Path.home() / ".openclaw" / "openforge"
    p.mkdir(parents=True, exist_ok=True)
    return p / "v07_chip_hits.json"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def bump(source: str = "files") -> None:
    """Best-effort: increment today's hit counter for `source`.

    `source` is a free-form tag (currently only "files" — list_files /
    read_file path). Multiple sources roll into the same counter but we
    keep the field for forward compatibility.
    """
    try:
        with _lock:
            path = _state_path()
            doc = {}
            if path.exists():
                try:
                    doc = json.loads(path.read_text(encoding="utf-8")) or {}
                except Exception:
                    doc = {}
            daily = doc.get("daily")
            if not isinstance(daily, dict):
                daily = {}
            key = _today()
            daily[key] = int(daily.get(key) or 0) + 1
            doc["daily"] = daily
            doc["total"] = int(doc.get("total") or 0) + 1
            if not doc.get("first_seen"):
                doc["first_seen"] = datetime.now(UTC).isoformat()
            doc["last_seen"] = datetime.now(UTC).isoformat()
            doc.setdefault("sources", {})
            doc["sources"][source] = int(doc["sources"].get(source) or 0) + 1
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, path)
    except Exception:
        # Never let telemetry break the API.
        pass


def snapshot() -> dict:
    """Return the current counter doc (empty dict if none yet)."""
    try:
        path = _state_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _reset_for_tests() -> None:
    try:
        p = _state_path()
        if p.exists():
            p.unlink()
    except Exception:
        pass
