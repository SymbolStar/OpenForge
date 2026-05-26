#!/usr/bin/env python3
"""scripts/dev_seed.py — populate a fresh OPENFORGE_DIR with fixture data.

Used by `forge dev` to bootstrap an isolated dev environment so judy (or any
contributor) can poke a real-looking instance without touching production
~/.openclaw/openforge/.

Idempotent: re-running won't duplicate threads; it only seeds when squads.json
is missing or `--force` is given.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-seed even if squads.json already exists")
    args = ap.parse_args()

    target = os.environ.get("OPENFORGE_DIR")
    if not target:
        print("[dev-seed] OPENFORGE_DIR is not set; refusing to run.", file=sys.stderr)
        print("[dev-seed] Set OPENFORGE_DIR=/tmp/openforge-dev (or similar).", file=sys.stderr)
        return 2
    target_dir = Path(target).expanduser()
    if not args.force and (target_dir / "squads.json").exists():
        print(f"[dev-seed] {target_dir}/squads.json exists; skip (use --force to reseed).")
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import forge_store as fs  # noqa: E402

    target_dir.mkdir(parents=True, exist_ok=True)

    # Wipe (only if forcing) — defensive: never touch outside dev dir.
    if args.force:
        for p in target_dir.rglob("*"):
            if p.is_file():
                p.unlink()
        for p in sorted(target_dir.glob("**/"), reverse=True):
            if p != target_dir and p.is_dir():
                try:
                    p.rmdir()
                except OSError:
                    pass

    # Seed one development-shaped squad pointing at the real OpenForge repo
    # (so the project_dir feature actually validates green out of the box),
    # plus a discussion-shaped squad with no project_dir (to exercise the
    # "rule-segment hidden" code path in PR-B).
    dev_repo = Path("/Volumes/DevDisk/symbol/openforge")
    fs.create_squad({
        "id": "dev_native",
        "name": "Dev · AI Native",
        "members": ["judy", "alice", "designer"],
        "chair": "judy",
        "emoji": "🛠",
        "description": "Dev fixture: mirrors ss_ai_native with project_dir set.",
        "project_dir": str(dev_repo) if dev_repo.exists() else None,
    })
    fs.create_squad({
        "id": "dev_chat",
        "name": "Dev · Discussion only",
        "members": ["alice", "judy"],
        "chair": "alice",
        "emoji": "💬",
        "description": "Dev fixture: no project_dir; preamble should stay quiet.",
    })

    # A couple of seed threads on the dev squad so the UI doesn't open empty.
    t1 = fs.create_thread("dev_native", "scott", title="Welcome to dev",
                          opening_content="This is a throw-away dev instance. Poke around freely.")
    fs.add_thread_post(t1["thread_id"], "judy",
                       "Real dev — `forge dev-reset` to wipe.")
    fs.create_thread("dev_chat", "scott", title="Discussion-only sanity",
                     opening_content="This squad has no project_dir; agent context bundle should hide the worktree-rule preamble.")

    # Tiny stale time so logs feel non-degenerate.
    time.sleep(0.01)

    print(f"[dev-seed] seeded {target_dir}")
    print(f"[dev-seed]   - dev_native (project_dir={dev_repo})")
    print("[dev-seed]   - dev_chat   (no project_dir)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
