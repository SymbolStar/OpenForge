#!/usr/bin/env python3
"""migrations/001_seed_ss_ai_native_project_dir.py — PR-A.

Sets ``ss_ai_native.project_dir`` to the OpenForge repo path so the squad
becomes "development squad" the moment PR-B router injection lands. Idempotent:
running it twice is a no-op once the value is already correct.

Usage::

    python migrations/001_seed_ss_ai_native_project_dir.py

Exits 0 always (no-op == success). Prints what it did to stdout.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SQUAD_ID = "ss_ai_native"
TARGET_PATH = "/Volumes/DevDisk/symbol/openforge"
SQUADS_JSON = Path.home() / ".openclaw" / "openforge" / "squads.json"


def main() -> int:
    if not SQUADS_JSON.exists():
        print(f"[migration 001] {SQUADS_JSON} does not exist; nothing to do.")
        return 0
    doc = json.loads(SQUADS_JSON.read_text(encoding="utf-8"))
    squads = doc.get("squads") or {}
    sq = squads.get(SQUAD_ID)
    if not sq:
        print(f"[migration 001] squad {SQUAD_ID!r} not found; nothing to do.")
        return 0
    current = sq.get("project_dir")
    if current == TARGET_PATH:
        print(f"[migration 001] {SQUAD_ID}.project_dir already = {TARGET_PATH}; no-op.")
        return 0
    sq["project_dir"] = TARGET_PATH
    squads[SQUAD_ID] = sq
    doc["squads"] = squads
    tmp = SQUADS_JSON.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, SQUADS_JSON)
    print(f"[migration 001] {SQUAD_ID}.project_dir: {current!r} -> {TARGET_PATH!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
