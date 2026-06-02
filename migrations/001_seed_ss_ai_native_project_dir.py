#!/usr/bin/env python3
"""migrations/001_seed_ss_ai_native_project_dir.py — PR-A.

Sets ``ss_ai_native.project_dir`` to the OpenForge repo path so the squad
becomes "development squad" the moment PR-B router injection lands.
Idempotent: running it twice is a no-op once the value is already correct.

The target path is resolved at run time, in priority order:

  1. ``$OPENFORGE_SS_AI_NATIVE_PROJECT_DIR`` env var — explicit override.
  2. ``$OPENFORGE_REPO_ROOT`` env var — if you keep all SymbolStar repos
     under one root, e.g. ``~/Symbolstarx``.
  3. The directory containing this migration's repo (``<repo>``).
     This is what "clone OpenForge anywhere and run it" should hit.

The old behavior hard-coded ``/Volumes/DevDisk/symbol/openforge``, which
broke the moment the DevDisk volume was unavailable. Never again.

Usage::

    python migrations/001_seed_ss_ai_native_project_dir.py

Exits 0 always (no-op == success). Prints what it did to stdout.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make `forge_paths` importable when this file is executed as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import forge_paths  # noqa: E402

SQUAD_ID = "ss_ai_native"
SQUADS_JSON = forge_paths.squads_path()


def _resolve_target_path() -> str:
    explicit = os.environ.get("OPENFORGE_SS_AI_NATIVE_PROJECT_DIR")
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    repo_root = os.environ.get("OPENFORGE_REPO_ROOT")
    if repo_root:
        return str((Path(repo_root).expanduser() / "openforge").resolve())
    # Default: the repo this migration lives in.
    return str(Path(__file__).resolve().parent.parent)


def main() -> int:
    target = _resolve_target_path()
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
    if current == target:
        print(f"[migration 001] {SQUAD_ID}.project_dir already = {target}; no-op.")
        return 0
    sq["project_dir"] = target
    squads[SQUAD_ID] = sq
    doc["squads"] = squads
    tmp = SQUADS_JSON.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, SQUADS_JSON)
    print(f"[migration 001] {SQUAD_ID}.project_dir: {current!r} -> {target!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
