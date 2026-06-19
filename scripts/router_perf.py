#!/usr/bin/env python3
"""router_perf.py - OpenForge router performance telemetry.

Reads ~/.openforge/threads/*/events.jsonl and reconstructs per-chip
latency for the four routing stages:

  (1) post_added (trigger) -> chip post_added (router pickup + placeholder write)
  (2) chip post_added      -> phase=running    (subprocess spawn + env + preamble)
  (3) phase=running        -> phase=done|failed|skipped (agent compute wall-clock)
  (4) full chip duration_ms                              (router-reported, ~= 2+3)

Usage:
  python3 scripts/router_perf.py                  # last 200 threads
  python3 scripts/router_perf.py --threads 500    # wider sample
  python3 scripts/router_perf.py --days 7         # only threads touched in last N days
  python3 scripts/router_perf.py --agent milk     # filter per-agent
  python3 scripts/router_perf.py --json           # machine-readable

Pure stdlib. Read-only. Safe to run while forge is live.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from glob import glob
from pathlib import Path

THREADS_DIR = Path.home() / ".openforge" / "threads"


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def collect(threads_dir, max_threads, days, agent_filter):
    files = sorted(glob(str(threads_dir / "*" / "events.jsonl")),
                   key=lambda f: os.path.getmtime(f), reverse=True)
    if days is not None:
        cutoff = time.time() - days * 86400
        files = [f for f in files if os.path.getmtime(f) >= cutoff]
    files = files[:max_threads]

    stage1, stage2, stage3, stage4 = [], [], [], []
    per_agent = defaultdict(list)
    phases = Counter()
    per_agent_fail = Counter()
    failures = defaultdict(list)

    for fp in files:
        post_ts = {}
        chips = {}
        try:
            fh = open(fp, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                k = ev.get("kind")
                ts = ev.get("ts")
                if k == "post_added":
                    pid = ev.get("post_id")
                    if pid:
                        post_ts[pid] = ts
                    if ev.get("post_type") == "status_chip":
                        chips[pid] = {
                            "created": ts,
                            "agent": ev.get("agent_id"),
                            "trigger": ev.get("trigger_post_id"),
                        }
                elif k == "post_updated":
                    pid = ev.get("post_id")
                    patch = ev.get("patch") or {}
                    if pid in chips:
                        ph = patch.get("phase")
                        if ph == "running" and "running" not in chips[pid]:
                            chips[pid]["running"] = ts
                        if ph in ("done", "failed", "skipped"):
                            chips[pid]["final"] = ts
                            chips[pid]["final_phase"] = ph
                            dm = patch.get("duration_ms")
                            if dm is not None:
                                chips[pid]["duration_ms"] = dm
                            err = patch.get("error")
                            if err and ph == "failed":
                                chips[pid]["error"] = err

        for pid, c in chips.items():
            ag = c.get("agent") or "?"
            if agent_filter and ag != agent_filter:
                continue
            fp_phase = c.get("final_phase")
            if fp_phase:
                phases[fp_phase] += 1
                if fp_phase == "failed":
                    per_agent_fail[ag] += 1
                    err = c.get("error")
                    if err:
                        failures[ag].append(err[:200])
            dm = c.get("duration_ms")
            if dm is not None:
                stage4.append(dm)
                per_agent[ag].append(dm)
            trig = c.get("trigger")
            if trig and trig in post_ts:
                t0 = _parse_ts(post_ts[trig])
                t1 = _parse_ts(c["created"])
                if t0 and t1:
                    dt = (t1 - t0).total_seconds() * 1000
                    if 0 <= dt < 60000:
                        stage1.append(dt)
            if "running" in c:
                t0 = _parse_ts(c["created"])
                t1 = _parse_ts(c["running"])
                if t0 and t1:
                    dt = (t1 - t0).total_seconds() * 1000
                    if 0 <= dt < 600000:
                        stage2.append(dt)
            if "running" in c and "final" in c:
                t0 = _parse_ts(c["running"])
                t1 = _parse_ts(c["final"])
                if t0 and t1:
                    dt = (t1 - t0).total_seconds() * 1000
                    if 0 <= dt < 3600000:
                        stage3.append(dt)

    return {
        "files_sampled": len(files),
        "stage1_ms": stage1,
        "stage2_ms": stage2,
        "stage3_ms": stage3,
        "stage4_ms": stage4,
        "phases": phases,
        "per_agent": per_agent,
        "per_agent_fail": per_agent_fail,
        "failures": failures,
    }


def _pct(arr, q):
    s = sorted(arr)
    return s[min(len(s) - 1, int(len(s) * q))]


def _fmt(ms):
    if ms >= 60_000:
        return "{:.1f}m".format(ms / 60_000)
    if ms >= 1000:
        return "{:.1f}s".format(ms / 1000)
    return "{:.0f}ms".format(ms)


def _row(name, arr):
    if not arr:
        return "  {:50s} n=0".format(name)
    return ("  {:50s} n={:4d}  med={:>8s}  p95={:>8s}  p99={:>8s}  max={:>8s}"
            .format(name, len(arr),
                    _fmt(st.median(arr)),
                    _fmt(_pct(arr, 0.95)),
                    _fmt(_pct(arr, 0.99)),
                    _fmt(max(arr))))


def render_text(d):
    lines = []
    lines.append("OpenForge router perf - sampled {} threads".format(d["files_sampled"]))
    total = sum(d["phases"].values())
    if total:
        ph = d["phases"]
        lines.append(
            "chips: {}   done={} ({}%)  skipped={} ({}%)  failed={} ({}%)".format(
                total,
                ph.get("done", 0), ph.get("done", 0) * 100 // total,
                ph.get("skipped", 0), ph.get("skipped", 0) * 100 // total,
                ph.get("failed", 0), ph.get("failed", 0) * 100 // total,
            ))
    lines.append("")
    lines.append("Latency stages (router-internal vs. agent compute):")
    lines.append(_row("(1) post_added -> chip_created  (router pickup)", d["stage1_ms"]))
    lines.append(_row("(2) chip_created -> running     (subprocess spawn)", d["stage2_ms"]))
    lines.append(_row("(3) running -> final            (agent compute)", d["stage3_ms"]))
    lines.append(_row("(4) chip.duration_ms            (router-reported)", d["stage4_ms"]))
    lines.append("")
    lines.append("Per-agent compute (using chip duration_ms):")
    lines.append("  {:16s} {:>4s}  {:>7s}  {:>7s}  {:>7s}  {:>7s}  fail".format(
        "agent", "n", "med", "p95", "p99", "max"))
    for ag, ds in sorted(d["per_agent"].items(), key=lambda x: -len(x[1])):
        if not ds:
            continue
        lines.append("  {:16s} {:>4d}  {:>7s}  {:>7s}  {:>7s}  {:>7s}  {}".format(
            ag, len(ds),
            _fmt(st.median(ds)),
            _fmt(_pct(ds, 0.95)),
            _fmt(_pct(ds, 0.99)),
            _fmt(max(ds)),
            d["per_agent_fail"].get(ag, 0),
        ))
    if d["failures"]:
        lines.append("")
        lines.append("Recent failure tails (truncated):")
        for ag, errs in d["failures"].items():
            for e in errs[-2:]:
                lines.append("  [{}] {}".format(ag, e))
    return chr(10).join(lines)


def render_json(d):
    out = {
        "files_sampled": d["files_sampled"],
        "phases": dict(d["phases"]),
        "stages": {},
        "per_agent": {},
    }
    for name, arr in [("stage1_pickup_ms", d["stage1_ms"]),
                      ("stage2_spawn_ms", d["stage2_ms"]),
                      ("stage3_compute_ms", d["stage3_ms"]),
                      ("stage4_chip_duration_ms", d["stage4_ms"])]:
        if arr:
            out["stages"][name] = {
                "n": len(arr),
                "median": int(st.median(arr)),
                "p95": int(_pct(arr, 0.95)),
                "p99": int(_pct(arr, 0.99)),
                "max": int(max(arr)),
            }
    for ag, ds in d["per_agent"].items():
        if not ds:
            continue
        out["per_agent"][ag] = {
            "n": len(ds),
            "median_ms": int(st.median(ds)),
            "p95_ms": int(_pct(ds, 0.95)),
            "p99_ms": int(_pct(ds, 0.99)),
            "max_ms": int(max(ds)),
            "fail_count": d["per_agent_fail"].get(ag, 0),
        }
    return json.dumps(out, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threads", type=int, default=200,
                    help="max threads to sample, newest first (default 200)")
    ap.add_argument("--days", type=float, default=None,
                    help="only threads touched in last N days")
    ap.add_argument("--agent", default=None, help="filter to a single agent id")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--threads-dir", default=str(THREADS_DIR),
                    help="override threads dir (default {})".format(THREADS_DIR))
    args = ap.parse_args()

    td = Path(args.threads_dir).expanduser()
    if not td.exists():
        print("threads dir not found: {}".format(td), file=sys.stderr)
        sys.exit(2)
    d = collect(td, args.threads, args.days, args.agent)
    if args.json:
        print(render_json(d))
    else:
        print(render_text(d))


if __name__ == "__main__":
    main()
