"""
forge_store.py — JSONL event store for OpenForge.

OpenForge: multi-agent task tracker. Threads are tasks; @ assigns the next agent.

Event log is the source of truth. Markdown is a derived view.

Event schema (one JSON object per line):
  {
    "id": "evt_<ts>_<rand>",
    "ts": "2026-05-15T14:50:05.123+08:00",
    "kind": "meeting_started" | "topic_started" | "post_added"
          | "post_superseded" | "meeting_finished" | "note",
    ... (kind-specific fields)
  }

Per-day directory:
  ~/.openclaw/standups/data/<YYYY-MM-DD>/
    ├── events.jsonl      # append-only, source of truth
    └── .lock             # fcntl advisory file lock for writers

The Markdown file (~/.openclaw/standups/standup-<date>.md)
is regenerated from events.jsonl after each write (idempotent).
"""

from __future__ import annotations

import datetime
import errno
import fcntl
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# ─── paths ────────────────────────────────────────────────────────────
STANDUP_DIR = Path.home() / ".openclaw" / "standups"
DATA_DIR = STANDUP_DIR / "data"

# Permissive across CJK + ascii word + dash; aligned across server/script/web.
AGENT_ID_RE = r"[\w\u4e00-\u9fff][-\w\u4e00-\u9fff]*"
MENTION_RE = re.compile(rf"@({AGENT_ID_RE})")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_valid_date(s: str) -> bool:
    if not isinstance(s, str) or not DATE_RE.match(s):
        return False
    try:
        datetime.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


def now_clock() -> str:
    return datetime.datetime.now().astimezone().strftime("%H:%M:%S")


def gen_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000):x}_{secrets.token_hex(3)}"


# ─── path helpers ─────────────────────────────────────────────────────
def day_dir(date: str) -> Path:
    if not is_valid_date(date):
        raise ValueError(f"invalid date: {date!r}")
    p = DATA_DIR / date
    p.mkdir(parents=True, exist_ok=True)
    return p


def events_path(date: str) -> Path:
    return day_dir(date) / "events.jsonl"


def lock_path(date: str) -> Path:
    return day_dir(date) / ".lock"


def md_path(date: str) -> Path:
    STANDUP_DIR.mkdir(parents=True, exist_ok=True)
    return STANDUP_DIR / f"standup-{date}.md"


# ─── locking ──────────────────────────────────────────────────────────
@contextmanager
def file_lock(date: str, exclusive: bool = True, timeout: float = 30.0):
    """fcntl advisory lock; LOCK_EX for writers, LOCK_SH for readers."""
    path = lock_path(date)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    flag |= fcntl.LOCK_NB
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, flag)
                break
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire {'EX' if exclusive else 'SH'} "
                        f"lock on {path} within {timeout}s"
                    )
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def is_locked_exclusive(date: str) -> bool:
    """True if someone currently holds an EX lock on this day's lock file."""
    path = lock_path(date)
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            raise
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


# ─── event io ─────────────────────────────────────────────────────────
def append_event(date: str, event: dict[str, Any]) -> dict[str, Any]:
    """Append one event under EX lock. Returns the stored event (with id/ts filled in)."""
    event = dict(event)
    event.setdefault("id", gen_id("evt"))
    event.setdefault("ts", now_iso())
    line = json.dumps(event, ensure_ascii=False)
    if "\n" in line:  # paranoia
        raise ValueError("event JSON contains literal newline")
    with file_lock(date, exclusive=True):
        with events_path(date).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    return event


def read_events(date: str) -> list[dict[str, Any]]:
    """Read all events under SH lock. Skips malformed/half-written tail lines."""
    path = events_path(date)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with file_lock(date, exclusive=False):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # tolerate the very last line being half-written;
                    # ignore mid-stream corruption with a warning marker.
                    continue
    return out


def list_dates() -> list[str]:
    if not DATA_DIR.exists():
        return []
    out = []
    for p in sorted(DATA_DIR.iterdir(), reverse=True):
        if p.is_dir() and is_valid_date(p.name) and (p / "events.jsonl").exists():
            out.append(p.name)
    return out


# ─── projections (events -> structured meeting) ───────────────────────
def project_meeting(date: str) -> dict[str, Any] | None:
    """Fold events into the meeting model the UI consumes."""
    events = read_events(date)
    if not events:
        return None

    meeting: dict[str, Any] = {
        "date": date,
        "title": date,
        "chair": "?",
        "members": [],
        "started_at": None,
        "ended_at": None,
        "topics": [],
        "topics_by_id": {},
        "posts_by_id": {},
        "superseded": set(),
        "raw_events": len(events),
    }

    for ev in events:
        kind = ev.get("kind")
        if kind == "meeting_started":
            meeting["chair"] = ev.get("chair", meeting["chair"])
            meeting["members"] = ev.get("members", meeting["members"])
            meeting["title"] = ev.get("title", meeting["title"])
            meeting["started_at"] = ev.get("ts")
        elif kind == "topic_started":
            tid = ev.get("topic_id") or gen_id("t")
            topic = {
                "id": tid,
                "idx": ev.get("idx", len(meeting["topics"]) + 1),
                "title": ev.get("title", "(untitled)"),
                "kind": ev.get("topic_kind", "topic"),
                "posts": [],
            }
            meeting["topics"].append(topic)
            meeting["topics_by_id"][tid] = topic
        elif kind == "post_added":
            tid = ev.get("topic_id")
            topic = meeting["topics_by_id"].get(tid)
            if topic is None:
                # synthesize an ad-hoc topic so we never lose data
                tid = tid or gen_id("t")
                topic = {
                    "id": tid, "idx": len(meeting["topics"]) + 1,
                    "title": "(orphan)", "kind": "other", "posts": [],
                }
                meeting["topics"].append(topic)
                meeting["topics_by_id"][tid] = topic
            post = {
                "id": ev.get("post_id") or ev["id"],
                "ts": ev.get("ts"),
                "time": _clock_from_ts(ev.get("ts")),
                "speaker": ev.get("speaker", "?"),
                "content": ev.get("content", ""),
                "mentions": ev.get("mentions") or
                            extract_mentions(ev.get("content", "")),
                "parent_post_id": ev.get("parent_post_id"),
                "topic_id": tid,
                "superseded": False,
            }
            meeting["posts_by_id"][post["id"]] = post
            topic["posts"].append(post)
        elif kind == "post_superseded":
            pid = ev.get("post_id")
            post = meeting["posts_by_id"].get(pid)
            if post:
                post["superseded"] = True
                post["superseded_by"] = ev.get("by_post_id")
                meeting["superseded"].add(pid)
        elif kind == "meeting_finished":
            meeting["ended_at"] = ev.get("ts")

    # cleanup non-serializable
    meeting["superseded"] = sorted(meeting["superseded"])
    # normalize topic indices
    for i, t in enumerate(meeting["topics"], 1):
        t["idx"] = i
    return meeting


def _clock_from_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except ValueError:
        return ts[:8]


def extract_mentions(text: str) -> list[str]:
    return list(dict.fromkeys(MENTION_RE.findall(text or "")))


# ─── markdown projection ──────────────────────────────────────────────
def render_markdown(date: str) -> str:
    m = project_meeting(date)
    if m is None:
        return ""
    lines = [
        f"# 晨会纪要 · {m['date']}",
        "",
        f"**主席**: {m['chair']} · **参会**: {', '.join(m['members'])}",
        "",
        "<!-- generated from events.jsonl; edits here will be overwritten -->",
        "",
        "---",
    ]
    for t in m["topics"]:
        lines += ["", f"## {t['title']}", ""]
        for p in t["posts"]:
            if p["superseded"]:
                continue
            lines += [
                f"#### {p['speaker']} · {p['time']}",
                "",
                p["content"].rstrip(),
                "",
            ]
    return "\n".join(lines).rstrip() + "\n"


def write_markdown(date: str) -> Path:
    """Atomically regenerate the human-readable markdown file."""
    target = md_path(date)
    text = render_markdown(date)
    if not text:
        if target.exists():
            target.unlink()
        return target
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return target


# ─── high-level helpers used by run_standup.py ────────────────────────
def start_meeting(date: str, chair: str, members: list[str], title: str | None = None) -> dict:
    return append_event(date, {
        "kind": "meeting_started",
        "date": date,
        "title": title or date,
        "chair": chair,
        "members": members,
    })


def start_topic(date: str, idx: int, title: str, kind: str = "topic",
                topic_id: str | None = None) -> str:
    tid = topic_id or f"t{idx}_{secrets.token_hex(2)}"
    append_event(date, {
        "kind": "topic_started",
        "topic_id": tid,
        "idx": idx,
        "title": title,
        "topic_kind": kind,
    })
    return tid


def add_post(date: str, topic_id: str, speaker: str, content: str,
             parent_post_id: str | None = None) -> dict:
    pid = gen_id("p")
    return append_event(date, {
        "kind": "post_added",
        "post_id": pid,
        "topic_id": topic_id,
        "speaker": speaker,
        "content": content,
        "mentions": extract_mentions(content),
        "parent_post_id": parent_post_id,
    })


def supersede_post(date: str, post_id: str, by_post_id: str | None = None) -> dict:
    return append_event(date, {
        "kind": "post_superseded",
        "post_id": post_id,
        "by_post_id": by_post_id,
    })


def finish_meeting(date: str) -> dict:
    return append_event(date, {"kind": "meeting_finished", "date": date})


# ─── meeting summary for /api/standups list ───────────────────────────
def summarize(date: str) -> dict[str, Any] | None:
    m = project_meeting(date)
    if m is None:
        return None
    topic_count = sum(1 for t in m["topics"] if t["kind"] == "topic")
    post_count = sum(len(t["posts"]) for t in m["topics"])
    return {
        "date": m["date"],
        "title": m["title"],
        "chair": m["chair"],
        "members": m["members"],
        "topic_count": topic_count,
        "section_count": len(m["topics"]),
        "post_count": post_count,
        "started_at": m["started_at"],
        "ended_at": m["ended_at"],
        "in_progress": m["ended_at"] is None,
    }


def iter_summaries() -> Iterator[dict]:
    for d in list_dates():
        s = summarize(d)
        if s:
            yield s


# ─── squads ───────────────────────────────────────────────────────────
SQUADS_PATH = STANDUP_DIR / "squads.json"
DEFAULT_SQUAD_ID = "milk-eng"

DEFAULT_SQUAD = {
    "id": DEFAULT_SQUAD_ID,
    "chair": "milk",
    "members": ["milk", "sentry", "bugfix", "milly", "kb"],
    "emoji": "🥛",
    "name": "milk 工程部",
    "description": "",
}


def _default_squads_doc() -> dict[str, Any]:
    return {"version": 1, "squads": {DEFAULT_SQUAD_ID: dict(DEFAULT_SQUAD)}}


def _write_squads_doc(doc: dict[str, Any]) -> None:
    STANDUP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SQUADS_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, SQUADS_PATH)


def ensure_default_squads() -> dict[str, Any]:
    """Create squads.json with the default squad when it is missing."""
    if not SQUADS_PATH.exists():
        doc = _default_squads_doc()
        _write_squads_doc(doc)
        return doc
    return _read_squads_doc()


def _read_squads_doc() -> dict[str, Any]:
    if not SQUADS_PATH.exists():
        return ensure_default_squads()
    with SQUADS_PATH.open("r", encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("version") != 1 or not isinstance(doc.get("squads"), dict):
        raise ValueError("invalid squads.json schema")
    return doc


def list_squads() -> list[dict[str, Any]]:
    doc = ensure_default_squads()
    return [dict(squad) for squad in doc["squads"].values()]


def get_squad(squad_id: str) -> dict[str, Any] | None:
    doc = ensure_default_squads()
    squad = doc["squads"].get(squad_id)
    return dict(squad) if squad else None


def create_squad(data: dict[str, Any]) -> dict[str, Any]:
    doc = ensure_default_squads()
    squad_id = data["id"]
    if squad_id in doc["squads"]:
        raise ValueError("squad already exists")
    squad = {
        "id": squad_id,
        "chair": data.get("chair") or data["members"][0],
        "members": list(data["members"]),
        "emoji": data.get("emoji") or "#",
        "name": data.get("name") or squad_id,
        "description": data.get("description") or "",
    }
    doc["squads"][squad_id] = squad
    _write_squads_doc(doc)
    return dict(squad)


def delete_squad(squad_id: str) -> bool:
    doc = ensure_default_squads()
    if squad_id not in doc["squads"]:
        return False
    del doc["squads"][squad_id]
    _write_squads_doc(doc)
    return True
