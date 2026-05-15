#!/usr/bin/env python3
"""
restore_main_session.py — emergency tool to restore an agent's main session
pointer after run_standup.py v0.3 overwrote it via `openclaw agent --session-id`.

Strategy: open the agent's sessions.json, find the most recent NON-standup
sessionId in the store (or use --target-session if explicitly given), and
rewrite agent:<agent>:main to point at it.

Usage:
  python3 restore_main_session.py --list                       # show diagnostics
  python3 restore_main_session.py --agent kb                   # auto-pick latest non-standup
  python3 restore_main_session.py --agent kb --target <uuid>   # explicit
  python3 restore_main_session.py --all                        # restore all 5 default agents

Always backs up the current sessions.json to /tmp/<agent>-sessions-<ts>.bak.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

DEFAULT_AGENTS = ["milk", "sentry", "bugfix", "milly", "kb"]
AGENTS_ROOT = Path.home() / ".openclaw" / "agents"

# anything we recognise as "synthetic standup session that should not be main"
TAINTED_RE = re.compile(r"^(standup-|huddle-)")


def sessions_path(agent: str) -> Path:
    return AGENTS_ROOT / agent / "sessions" / "sessions.json"


def list_session_files(agent: str) -> list[tuple[str, float, int]]:
    """Return [(sessionId, mtime, size_bytes)] for plain (non-derived) jsonl files."""
    sess_dir = AGENTS_ROOT / agent / "sessions"
    out = []
    if not sess_dir.exists():
        return out
    for p in sess_dir.iterdir():
        if not p.is_file():
            continue
        name = p.name
        if not name.endswith(".jsonl"):
            continue
        if any(s in name for s in (".trajectory", ".checkpoint", ".reset")):
            continue
        if "trajectory-path" in name:
            continue
        sid = name[:-len(".jsonl")]
        out.append((sid, p.stat().st_mtime, p.stat().st_size))
    out.sort(key=lambda r: r[1], reverse=True)
    return out


def diagnose(agent: str) -> dict:
    sp = sessions_path(agent)
    if not sp.exists():
        return {"agent": agent, "error": "sessions.json missing"}
    store = json.loads(sp.read_text())
    main = store.get(f"agent:{agent}:main", {})
    files = list_session_files(agent)
    candidates = [(sid, mt, sz) for sid, mt, sz in files
                  if not TAINTED_RE.match(sid)]
    return {
        "agent": agent,
        "current_main_sid": main.get("sessionId"),
        "current_main_file": (main.get("sessionFile") or "").split("/")[-1],
        "tainted": bool(TAINTED_RE.match(main.get("sessionId") or "")),
        "best_candidate": candidates[0] if candidates else None,
        "candidates": [(sid, mt, sz) for sid, mt, sz in candidates[:5]],
    }


def print_diag(d: dict):
    print(f"\n━━━ {d['agent']} ━━━")
    if "error" in d:
        print(f"  ERROR: {d['error']}")
        return
    flag = "🔴 tainted" if d["tainted"] else "✅ ok"
    print(f"  current main: {d['current_main_sid']}  [{flag}]")
    if d["best_candidate"]:
        sid, mt, sz = d["best_candidate"]
        from datetime import datetime as dt
        print(f"  best candidate: {sid}")
        print(f"      mtime: {dt.fromtimestamp(mt).strftime('%Y-%m-%d %H:%M:%S')}  "
              f"size: {sz:,} bytes")
        if len(d["candidates"]) > 1:
            print(f"  other candidates ({len(d['candidates']) - 1}):")
            for sid, mt, sz in d["candidates"][1:]:
                print(f"      {sid}  ({dt.fromtimestamp(mt).strftime('%m-%d %H:%M')}, "
                      f"{sz:,}b)")


def restore(agent: str, target_sid: str | None = None,
            dry_run: bool = False) -> bool:
    sp = sessions_path(agent)
    if not sp.exists():
        print(f"❌ {agent}: sessions.json missing")
        return False

    store = json.loads(sp.read_text())
    key = f"agent:{agent}:main"
    main = store.get(key)
    if not main:
        print(f"❌ {agent}: no '{key}' entry")
        return False

    if target_sid is None:
        files = [(sid, mt) for sid, mt, _ in list_session_files(agent)
                 if not TAINTED_RE.match(sid)]
        if not files:
            print(f"❌ {agent}: no untainted candidate found")
            return False
        target_sid = files[0][0]
        print(f"  picked latest untainted: {target_sid}")

    target_file = AGENTS_ROOT / agent / "sessions" / f"{target_sid}.jsonl"
    if not target_file.exists():
        print(f"❌ {agent}: target file missing: {target_file}")
        return False

    if dry_run:
        print(f"  [dry-run] would set main → {target_sid}")
        return True

    # backup
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = Path(f"/tmp/{agent}-sessions-{ts}.bak.json")
    shutil.copy2(sp, backup)
    print(f"  backup: {backup}")

    # rewrite main pointer
    main["sessionId"] = target_sid
    main["sessionFile"] = str(target_file)
    main["updatedAt"] = int(time.time() * 1000)
    main["restoredFromTainted"] = True
    main["restoredAt"] = ts
    store[key] = main

    tmp = sp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2))
    os.replace(tmp, sp)
    print(f"  ✅ {agent}: main → {target_sid}")
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--agent")
    p.add_argument("--target", help="explicit target session id (no `.jsonl`)")
    p.add_argument("--all", action="store_true",
                   help=f"restore all default agents: {DEFAULT_AGENTS}")
    p.add_argument("--list", action="store_true",
                   help="diagnose only, do not modify")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.list or (not args.agent and not args.all):
        targets = [args.agent] if args.agent else DEFAULT_AGENTS
        for a in targets:
            print_diag(diagnose(a))
        if not args.list and not args.agent and not args.all:
            print("\n(use --agent <id> or --all to actually restore)")
        return

    if args.all:
        for a in DEFAULT_AGENTS:
            d = diagnose(a)
            if d.get("tainted"):
                restore(a, dry_run=args.dry_run)
            else:
                print(f"✅ {a}: not tainted, skipping")
        return

    restore(args.agent, args.target, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
