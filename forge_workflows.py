"""Workflows tab (v0.1) data layer.

Talks to the local `openclaw cron` CLI to enumerate scheduled jobs
and normalize them into the shape the front-end (`web/app.js` →
`workflowsView`) expects.

Design owner: designer / issue #115.  v0.1 is a UI shell — we do NOT
mutate ~/.openclaw/cron/jobs.json here, only *read* via `cron list
--json` and *trigger* immediate runs via `cron run --id <id>`.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


CRON_BIN = "openclaw"
CLI_TIMEOUT = 8.0  # seconds; the local CLI is fast, we bail early on hang


# ─── low-level CLI adapters ──────────────────────────────────────────

def _run_cli(args: list[str]) -> dict:
    """Run `openclaw <args>` and parse stdout as JSON.

    Raises RuntimeError with the collected stderr snippet on failure so
    the HTTP layer can surface a useful message instead of a 500.
    """
    try:
        proc = subprocess.run(
            [CRON_BIN, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"openclaw CLI not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"openclaw {' '.join(args)} timed out") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"openclaw {' '.join(args)} exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
        )
    out = (proc.stdout or "").strip()
    if not out or out[0] not in "{[":
        raise RuntimeError(
            f"openclaw {' '.join(args)} produced non-JSON output: "
            f"{out[:200]!r}"
        )
    return json.loads(out)


# ─── schedule → 人话 ────────────────────────────────────────────────

def _cron_to_human(expr: str) -> str:
    """Map the 5 patterns we care about; fall back to raw."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return expr
    m, h, dom, mon, dow = parts
    # 只覆盖 5 条规则；其他 fallback raw
    if dom == "*" and mon == "*" and dow == "*":
        try:
            mi = int(m)
            hi = int(h)
        except ValueError:
            return expr
        return f"每天 {hi:02d}:{mi:02d}"
    if dom == "*" and mon == "*" and dow in ("1-5", "1,2,3,4,5"):
        try:
            mi = int(m); hi = int(h)
        except ValueError:
            return expr
        return f"工作日 {hi:02d}:{mi:02d}"
    if dom == "*" and mon == "*" and dow in ("6,0", "0,6", "6-7", "0,6,7"):
        try:
            mi = int(m); hi = int(h)
        except ValueError:
            return expr
        return f"周末 {hi:02d}:{mi:02d}"
    return expr


def _next_fire_for_cron(expr: str, tz_name: str, from_ms: int | None = None) -> int | None:
    """Compute the next-fire time for our supported patterns.

    Returns ms since epoch, or None if unsupported (fallback: use
    whatever `state.nextRunAtMs` already exists on the job).
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    m, h, dom, mon, dow = parts
    try:
        mi = int(m); hi = int(h)
    except ValueError:
        return None
    if dom != "*" or mon != "*":
        return None
    try:
        tz = ZoneInfo(tz_name or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    now = datetime.fromtimestamp((from_ms or int(time.time() * 1000)) / 1000, tz=tz)
    candidate = now.replace(hour=hi, minute=mi, second=0, microsecond=0)
    for _ in range(14):
        weekday = candidate.weekday()  # 0=Mon..6=Sun
        ok = False
        if dow == "*":
            ok = True
        elif dow in ("1-5", "1,2,3,4,5"):
            ok = weekday <= 4
        elif dow in ("6,0", "0,6", "6-7", "0,6,7"):
            ok = weekday in (5, 6)
        else:
            return None
        if ok and candidate > now:
            return int(candidate.timestamp() * 1000)
        candidate = candidate + timedelta(days=1)
    return None


def _human_next(ms: int | None, tz_name: str) -> str:
    if not ms:
        return "—"
    try:
        tz = ZoneInfo(tz_name or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz=tz)
    dt = datetime.fromtimestamp(ms / 1000, tz=tz)
    same_day = dt.date() == now.date()
    delta = ms - int(now.timestamp() * 1000)
    if delta < 0:
        return "已过期"
    total_min = delta // 60000
    hours = total_min // 60
    mins = total_min % 60
    if same_day:
        prefix = f"今天 {dt.strftime('%H:%M')}"
    elif dt.date() == (now.date() + timedelta(days=1)):
        prefix = f"明天 {dt.strftime('%H:%M')}"
    else:
        prefix = dt.strftime("%m-%d %H:%M")
    if hours >= 24:
        days = hours // 24
        rem_h = hours % 24
        rel = f"in {days}d {rem_h}h" if rem_h else f"in {days}d"
    elif hours >= 1:
        rel = f"in {hours}h {mins}m" if mins else f"in {hours}h"
    else:
        rel = f"in {mins}m"
    return f"{prefix} · {rel}"


# ─── target / delivery labels ────────────────────────────────────────

def _target_labels(session_target: str | None) -> tuple[str, str, str]:
    """(kind, label, labelShort) for a job's sessionTarget."""
    t = session_target or "isolated"
    if t == "isolated":
        return ("isolated", "isolated", "isolated")
    if t == "current":
        return ("current", "current", "current")
    if t.startswith("session:"):
        rest = t.split(":", 1)[1]
        segs = rest.split(":")
        tail = segs[-1] if segs else rest
        if len(tail) > 20:
            tail = tail[:8] + "…" + tail[-8:]
        short = f"session:…{tail}"
        return ("session", t, short)
    return ("session", t, t[:32])


# ─── normalize one job ───────────────────────────────────────────────

def _normalize(job: dict) -> dict:
    sched = job.get("schedule") or {}
    kind = sched.get("kind") or "cron"
    tz = sched.get("tz") or "Asia/Shanghai"
    raw = sched.get("expr") or sched.get("at") or ""
    state = job.get("state") or {}

    if kind == "cron":
        human = _cron_to_human(raw)
        next_ms = _next_fire_for_cron(raw, tz)
        if next_ms is None:
            next_ms = state.get("nextRunAtMs")
    elif kind == "at":
        at_val = sched.get("at") or raw
        # `at` may be ISO string or ms
        next_ms = None
        try:
            if isinstance(at_val, (int, float)):
                next_ms = int(at_val)
            elif isinstance(at_val, str):
                try:
                    next_ms = int(at_val)
                except ValueError:
                    dt = datetime.fromisoformat(at_val.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    next_ms = int(dt.timestamp() * 1000)
        except Exception:
            next_ms = state.get("nextRunAtMs")
        now = int(time.time() * 1000)
        if next_ms and next_ms >= now:
            human = f"一次性 · {datetime.fromtimestamp(next_ms/1000, tz=ZoneInfo(tz)).strftime('%Y-%m-%d %H:%M')}"
        else:
            human = "一次性 · 已过期"
            next_ms = None
    else:
        human = raw or kind
        next_ms = state.get("nextRunAtMs")

    kind_t, label, label_short = _target_labels(job.get("sessionTarget"))
    delivery = job.get("delivery") or {}

    last_run = None
    lr_status = state.get("lastRunStatus") or state.get("lastStatus")
    if state.get("lastRunAtMs") or lr_status:
        raw_status = (lr_status or "").lower()
        if raw_status == "error":
            mapped = "failed"
        elif raw_status in ("ok", "success"):
            mapped = "ok"
        elif raw_status == "running":
            mapped = "running"
        elif raw_status == "skipped":
            mapped = "ok"  # skipped shown as neutral; disabled state carries the off signal
        else:
            mapped = raw_status or "ok"
        last_run = {
            "status": mapped,
            "startedAt": state.get("lastRunAtMs"),
            "durationMs": state.get("lastDurationMs"),
            "resultDetail": state.get("lastDiagnosticSummary"),
        }
        if state.get("lastError") and mapped == "failed":
            # Trim: lastError tends to be long; UI has a small cell.
            err = str(state["lastError"])
            if len(err) > 80:
                err = err[:80] + "…"
            last_run["resultDetail"] = err

    return {
        "id": job.get("id"),
        "name": job.get("name") or job.get("id"),
        "agent": job.get("agentId") or "?",
        "enabled": bool(job.get("enabled", True)),
        "schedule": {
            "kind": kind,
            "human": human,
            "tz": tz,
            "raw": raw,
            "nextRunAt": next_ms,
            "nextRunHuman": _human_next(next_ms, tz),
        },
        "target": {"kind": kind_t, "label": label, "labelShort": label_short},
        "delivery": {"mode": delivery.get("mode") or "none", "to": delivery.get("to")},
        "lastRun": last_run,
    }


# ─── public API ──────────────────────────────────────────────────────

def list_workflows() -> dict:
    """Return {jobs: [NormalizedJob, ...]} for the front-end."""
    data = _run_cli(["cron", "list", "--json"])
    raw_jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(raw_jobs, list):
        return {"jobs": []}
    out = []
    for j in raw_jobs:
        try:
            out.append(_normalize(j))
        except Exception as e:  # keep the response alive if one job is malformed
            out.append({
                "id": j.get("id"),
                "name": j.get("name") or "(malformed)",
                "agent": j.get("agentId") or "?",
                "enabled": bool(j.get("enabled", False)),
                "schedule": {"kind": "cron", "human": "(parse error)", "tz": "Asia/Shanghai",
                             "raw": str(e)[:80], "nextRunAt": None, "nextRunHuman": "—"},
                "target": {"kind": "isolated", "label": "?", "labelShort": "?"},
                "delivery": {"mode": "none"},
                "lastRun": None,
            })
    return {"jobs": out}


def run_now(job_id: str) -> dict:
    """Trigger an immediate run for a single job.

    Returns {"ok": True} on success; raises RuntimeError otherwise.
    """
    if not job_id or not isinstance(job_id, str):
        raise RuntimeError("job id required")
    _run_cli(["cron", "run", "--id", job_id])
    return {"ok": True}
