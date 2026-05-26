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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# ─── paths ────────────────────────────────────────────────────────────
# v0.4 layout (thread-first, Slack-shaped):
#   ~/.openclaw/openforge/
#     ├── squads.json
#     ├── threads/<thread-id>/
#     │     ├── events.jsonl
#     │     ├── .lock
#     │     └── thread.md
#     └── (legacy) ../standups/  ← still readable for old standup runs
#
# v0.5: OPENFORGE_DIR env var overrides the default location so `forge dev`
# (or any other isolated invocation) can run against a throw-away fixture
# tree without touching real user state.
_env_forge_dir = os.environ.get("OPENFORGE_DIR")
if _env_forge_dir:
    FORGE_DIR = Path(_env_forge_dir).expanduser()
else:
    FORGE_DIR = Path.home() / ".openclaw" / "openforge"
THREADS_DIR = FORGE_DIR / "threads"

# Legacy standup layout (read-only; kept for projection back-compat).
STANDUP_DIR = Path.home() / ".openclaw" / "standups"
DATA_DIR = STANDUP_DIR / "data"

THREAD_ID_RE = re.compile(r"^th_[0-9a-f]+_[0-9a-f]+$")

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


# ─── thread paths (v0.4) ──────────────────────────────────────────────
def thread_dir(thread_id: str) -> Path:
    if not THREAD_ID_RE.match(thread_id):
        raise ValueError(f"invalid thread_id: {thread_id!r}")
    p = THREADS_DIR / thread_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def thread_events_path(thread_id: str) -> Path:
    return thread_dir(thread_id) / "events.jsonl"


def thread_lock_path(thread_id: str) -> Path:
    return thread_dir(thread_id) / ".lock"


def thread_md_path(thread_id: str) -> Path:
    return thread_dir(thread_id) / "thread.md"


# ─── locking ──────────────────────────────────────────────────────────
@contextmanager
def _flock(path: Path, exclusive: bool, timeout: float):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    flag = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
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


@contextmanager
def thread_lock(thread_id: str, exclusive: bool = True, timeout: float = 30.0):
    with _flock(thread_lock_path(thread_id), exclusive, timeout):
        yield


@contextmanager
def file_lock(date: str, exclusive: bool = True, timeout: float = 30.0):
    """Legacy date-keyed lock for the standup pathway."""
    with _flock(lock_path(date), exclusive, timeout):
        yield


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


# ─── thread event io ──────────────────────────────────────────────────
def append_thread_event(thread_id: str, event: dict[str, Any]) -> dict[str, Any]:
    event = dict(event)
    event.setdefault("id", gen_id("evt"))
    event.setdefault("ts", now_iso())
    line = json.dumps(event, ensure_ascii=False)
    if "\n" in line:
        raise ValueError("event JSON contains literal newline")
    with thread_lock(thread_id, exclusive=True):
        with thread_events_path(thread_id).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    # SSE: notify any live subscribers AFTER the file lock is released so
    # downstream HTTP handlers can refetch the projected thread without
    # contending with the writer.
    try:
        _publish_thread_event(thread_id, event)
    except Exception:
        pass
    return event


# ─── in-memory pub-sub for SSE (P1) ──────────────────────────────────
# A tiny per-process broker: subscribers register a queue keyed by
# thread_id; every successful `append_thread_event` publishes the freshly
# written event to all live subscribers of that thread. Bounded queue so
# a stuck client cannot blow up server memory.
import threading as _threading_sse  # local alias to avoid touching top imports
from queue import Queue as _SseQueue

_sse_subs_lock = _threading_sse.Lock()
_sse_subscribers: dict[str, set[_SseQueue]] = {}


def subscribe_thread(thread_id: str, maxsize: int = 256) -> _SseQueue:
    q: _SseQueue = _SseQueue(maxsize=maxsize)
    with _sse_subs_lock:
        _sse_subscribers.setdefault(thread_id, set()).add(q)
    return q


def unsubscribe_thread(thread_id: str, q: _SseQueue) -> None:
    with _sse_subs_lock:
        bucket = _sse_subscribers.get(thread_id)
        if not bucket:
            return
        bucket.discard(q)
        if not bucket:
            _sse_subscribers.pop(thread_id, None)


def _publish_thread_event(thread_id: str, event: dict[str, Any]) -> None:
    with _sse_subs_lock:
        subs = list(_sse_subscribers.get(thread_id) or ())
    for q in subs:
        try:
            q.put_nowait(event)
        except Exception:
            # Subscriber's queue is full or broken — drop on the floor;
            # the client will fall back to the periodic poll.
            pass


def read_thread_events(thread_id: str) -> list[dict[str, Any]]:
    path = thread_events_path(thread_id)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with thread_lock(thread_id, exclusive=False):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
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
SQUADS_PATH = FORGE_DIR / "squads.json"
LEGACY_SQUADS_PATH = STANDUP_DIR / "squads.json"
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
    # Plan C: do NOT seed any default squad. Users decide what to create.
    return {"version": 1, "squads": {}}


def _write_squads_doc(doc: dict[str, Any]) -> None:
    FORGE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SQUADS_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, SQUADS_PATH)


def ensure_default_squads() -> dict[str, Any]:
    """Ensure squads.json exists (Plan C: may be empty).

    Migration: if a legacy ~/.openclaw/standups/squads.json exists and the
    new location does not, copy it over once.
    """
    if not SQUADS_PATH.exists() and LEGACY_SQUADS_PATH.exists():
        FORGE_DIR.mkdir(parents=True, exist_ok=True)
        SQUADS_PATH.write_bytes(LEGACY_SQUADS_PATH.read_bytes())
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
    # Graceful migration: existing squads.json may pre-date PR-A and lack
    # `project_dir`. Inject the default lazily on read so legacy docs round-
    # trip without manual migration. We *do not* persist here; the field is
    # written back on the next update_squad/create_squad. v0.5 PR-A.
    for squad in doc["squads"].values():
        if isinstance(squad, dict) and "project_dir" not in squad:
            squad["project_dir"] = None
    return doc


def list_squads(include_archived: bool = False) -> list[dict[str, Any]]:
    doc = ensure_default_squads()
    out = [dict(squad) for squad in doc["squads"].values()]
    if not include_archived:
        out = [s for s in out if not s.get("archived")]
    return out


def get_squad(squad_id: str) -> dict[str, Any] | None:
    doc = ensure_default_squads()
    squad = doc["squads"].get(squad_id)
    return dict(squad) if squad else None


def create_squad(data: dict[str, Any]) -> dict[str, Any]:
    doc = ensure_default_squads()
    squad_id = data["id"]
    if squad_id in doc["squads"]:
        raise ValueError("squad already exists")
    pd = data.get("project_dir")
    if pd is not None and not isinstance(pd, str):
        raise ValueError("project_dir must be a string or null")
    if isinstance(pd, str):
        pd = pd.strip() or None
    squad = {
        "id": squad_id,
        "chair": data.get("chair") or data["members"][0],
        "members": list(data["members"]),
        "emoji": data.get("emoji") or "#",
        "name": data.get("name") or squad_id,
        "description": data.get("description") or "",
        # v0.5 PR-A: optional absolute filesystem path that pins this squad to
        # a target git repo. None / empty means "discussion-only squad".
        "project_dir": pd,
    }
    doc["squads"][squad_id] = squad
    _write_squads_doc(doc)
    return dict(squad)


def update_squad(squad_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    doc = ensure_default_squads()
    cur = doc["squads"].get(squad_id)
    if not cur:
        return None
    # id is immutable; everything else is replaceable
    if "name" in patch and patch["name"]:
        cur["name"] = str(patch["name"])
    if "description" in patch:
        cur["description"] = str(patch["description"] or "")
    if "emoji" in patch:
        cur["emoji"] = str(patch["emoji"] or "#")
    if "members" in patch and isinstance(patch["members"], list) and patch["members"]:
        cur["members"] = [str(m) for m in patch["members"]]
    if "archived" in patch:
        cur["archived"] = bool(patch["archived"])
    if "project_dir" in patch:
        # PR-A: accept None / empty string (clears) or a non-empty string
        # (must be absolute). Existence + git-repo check is NOT enforced at
        # write time per PRD §5.1; that’s a derived runtime concern.
        raw_pd = patch["project_dir"]
        if raw_pd is None:
            cur["project_dir"] = None
        elif isinstance(raw_pd, str):
            v = raw_pd.strip()
            if not v:
                cur["project_dir"] = None
            else:
                cur["project_dir"] = v
        else:
            raise ValueError("project_dir must be a string or null")
    if "chair" in patch and patch["chair"]:
        if patch["chair"] not in cur["members"]:
            raise ValueError("chair must be a member")
        cur["chair"] = str(patch["chair"])
    elif cur["chair"] not in cur["members"]:
        # if members shrunk and chair fell out, snap chair to first member
        cur["chair"] = cur["members"][0]
    doc["squads"][squad_id] = cur
    _write_squads_doc(doc)
    return dict(cur)


def delete_squad(squad_id: str) -> bool:
    doc = ensure_default_squads()
    if squad_id not in doc["squads"]:
        return False
    del doc["squads"][squad_id]
    _write_squads_doc(doc)
    return True


# ─── threads (v0.4) ────────────────────────────────────────────────────────
#
# A thread is a Slack-shaped bounded topic. Stored at:
#   ~/.openclaw/openforge/threads/<thread_id>/events.jsonl
#
# Event kinds:
#   thread_started     { thread_id, squad_id, created_by }
#   post_added         { post_id, speaker, content, mentions[], parent_post_id }
#   post_superseded    { post_id, by_post_id }
#   reaction_added     { post_id, emoji, actor }
#   reaction_removed   { post_id, emoji, actor }
#   thread_closed      { thread_id, closed_by }
#
# No title, no topics, no date. Preview = first ~80 chars of opening post.


def new_thread_id() -> str:
    return gen_id("th")


def list_thread_ids() -> list[str]:
    if not THREADS_DIR.exists():
        return []
    out = []
    for p in THREADS_DIR.iterdir():
        if p.is_dir() and THREAD_ID_RE.match(p.name) and (p / "events.jsonl").exists():
            out.append(p.name)
    return out


# Title is a v0.10 addition for the "new thread" modal entry point. It is
# optional for back-compat — legacy create_thread(squad, by, content) calls
# (passing opening post as 3rd arg) still work and auto-derive the title.
THREAD_TITLE_MAX = 80


def _normalize_thread_title(title: str | None) -> str:
    if title is None:
        return ""
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    t = title.strip()
    if len(t) > THREAD_TITLE_MAX:
        raise ValueError(f"title too long (>{THREAD_TITLE_MAX} chars)")
    return t


def create_thread(squad_id: str,
                  created_by: str,
                  title_or_content: str | None = None,
                  *,
                  title: str | None = None,
                  opening_content: str | None = None) -> dict:
    """Create a new thread.

    Two call shapes are supported:

    1. v0.10 keyword form (preferred):
           create_thread(squad, by, title="…", opening_content="…" or None)
       The thread always gets an explicit title; opening_content is optional.

    2. Legacy positional form (for back-compat with existing tests/callers):
           create_thread(squad, by, opening_content_str)
       The 3rd positional arg is treated as the opening post; title is
       derived from the opening line (truncated to 80 chars).
    """
    if title is None and opening_content is None:
        # legacy form: third arg is opening content
        if not isinstance(title_or_content, str) or not title_or_content.strip():
            raise ValueError("opening content must be a non-empty string")
        opening_content = title_or_content
        # derive title from first line, truncated
        first_line = opening_content.strip().splitlines()[0]
        title = first_line[:THREAD_TITLE_MAX]
    else:
        if title is None:
            title = title_or_content
        title = _normalize_thread_title(title)
        if opening_content is not None and not isinstance(opening_content, str):
            raise ValueError("opening_content must be a string or None")
        if opening_content is not None and not opening_content.strip():
            opening_content = None
    if not get_squad(squad_id):
        raise ValueError(f"unknown squad: {squad_id!r}")
    speaker = (created_by or "scott").strip() or "scott"
    tid = new_thread_id()
    # bootstrap dir + first events
    thread_dir(tid)
    started_event = {
        "kind": "thread_started",
        "thread_id": tid,
        "squad_id": squad_id,
        "created_by": speaker,
    }
    if title:
        started_event["title"] = title
    append_thread_event(tid, started_event)
    if opening_content is not None:
        add_thread_post(tid, speaker, opening_content)
    return project_thread(tid)


def add_thread_post(thread_id: str, speaker: str, content: str,
                    parent_post_id: str | None = None) -> dict:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("post content must be a non-empty string")
    pid = gen_id("p")
    append_thread_event(thread_id, {
        "kind": "post_added",
        "post_id": pid,
        "speaker": (speaker or "scott").strip() or "scott",
        "content": content,
        "mentions": extract_mentions(content),
        "parent_post_id": parent_post_id,
    })
    return {"post_id": pid}


def supersede_thread_post(thread_id: str, post_id: str,
                          by_post_id: str | None = None) -> dict:
    return append_thread_event(thread_id, {
        "kind": "post_superseded",
        "post_id": post_id,
        "by_post_id": by_post_id,
    })


def close_thread(thread_id: str, closed_by: str = "scott") -> dict:
    return append_thread_event(thread_id, {
        "kind": "thread_closed",
        "thread_id": thread_id,
        "closed_by": closed_by or "scott",
    })


def set_thread_title(thread_id: str, title: str) -> dict:
    """Append a thread_titled event to rename a thread. v0.10."""
    t = _normalize_thread_title(title)
    return append_thread_event(thread_id, {
        "kind": "thread_titled",
        "thread_id": thread_id,
        "title": t,
    })


# Reactions: stored as discrete events; projection aggregates them onto the
# post as {emoji: [actor, ...]}. Toggle semantics live in add_reaction below.
_REACTION_EMOJI_RE = re.compile(r"^[\S\u200d]{1,16}$")


def _normalize_emoji(emoji: str) -> str:
    if not isinstance(emoji, str):
        raise ValueError("emoji must be a string")
    e = emoji.strip()
    if not e or not _REACTION_EMOJI_RE.match(e):
        raise ValueError("emoji must be 1-16 non-space chars")
    return e


def _post_reactions_map(thread_id: str, post_id: str) -> dict[str, list[str]]:
    """Replay only the reactions for a given post."""
    out: dict[str, list[str]] = {}
    for ev in read_thread_events(thread_id):
        if ev.get("post_id") != post_id:
            continue
        kind = ev.get("kind")
        if kind not in ("reaction_added", "reaction_removed"):
            continue
        emoji = ev.get("emoji")
        actor = ev.get("actor") or "scott"
        if not emoji:
            continue
        actors = out.setdefault(emoji, [])
        if kind == "reaction_added" and actor not in actors:
            actors.append(actor)
        elif kind == "reaction_removed" and actor in actors:
            actors.remove(actor)
            if not actors:
                out.pop(emoji, None)
    return out


def toggle_reaction(thread_id: str, post_id: str, emoji: str,
                    actor: str = "scott") -> dict:
    """Toggle a single (actor, emoji) reaction on a post. Returns refreshed map."""
    emoji = _normalize_emoji(emoji)
    actor = (actor or "scott").strip() or "scott"
    # validate post exists
    model = project_thread(thread_id)
    if model is None:
        raise ValueError(f"unknown thread: {thread_id!r}")
    if post_id not in model.get("posts_by_id", {}):
        raise ValueError(f"unknown post: {post_id!r}")
    current = _post_reactions_map(thread_id, post_id)
    already = actor in current.get(emoji, [])
    append_thread_event(thread_id, {
        "kind": "reaction_removed" if already else "reaction_added",
        "post_id": post_id,
        "emoji": emoji,
        "actor": actor,
    })
    return _post_reactions_map(thread_id, post_id)


def _preview_from(text: str, n: int = 80) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= n else line[: n - 1] + "…"


def project_thread(thread_id: str) -> dict[str, Any] | None:
    events = read_thread_events(thread_id)
    if not events:
        return None
    model: dict[str, Any] = {
        "thread_id": thread_id,
        "squad_id": None,
        "title": "",
        "created_by": "scott",
        "started_at": None,
        "closed_at": None,
        "closed_by": None,
        "participants": [],
        "posts": [],
        "posts_by_id": {},
        "superseded": set(),
        # v0.5 PR-A: store-only field. Not exposed via API/UI in this PR;
        # router will start reading it in PR-B.
        "extra_projects": [],
        "raw_events": len(events),
    }
    seen_participants: list[str] = []

    for ev in events:
        kind = ev.get("kind")
        if kind == "thread_started":
            model["squad_id"] = ev.get("squad_id")
            model["created_by"] = ev.get("created_by") or model["created_by"]
            model["started_at"] = ev.get("ts")
            t = ev.get("title")
            if isinstance(t, str) and t.strip():
                model["title"] = t.strip()
        elif kind == "thread_titled":
            t = ev.get("title")
            if isinstance(t, str):
                model["title"] = t.strip()
        elif kind == "post_added":
            post = {
                "id": ev.get("post_id") or ev["id"],
                "ts": ev.get("ts"),
                "time": _clock_from_ts(ev.get("ts")),
                "speaker": ev.get("speaker", "?"),
                "content": ev.get("content", ""),
                "mentions": ev.get("mentions") or extract_mentions(ev.get("content", "")),
                "parent_post_id": ev.get("parent_post_id"),
                "superseded": False,
                "reactions": {},
            }
            model["posts_by_id"][post["id"]] = post
            model["posts"].append(post)
            spk = post["speaker"]
            if spk and spk not in seen_participants:
                seen_participants.append(spk)
        elif kind == "post_superseded":
            pid = ev.get("post_id")
            p = model["posts_by_id"].get(pid)
            if p:
                p["superseded"] = True
                p["superseded_by"] = ev.get("by_post_id")
                model["superseded"].add(pid)
        elif kind == "reaction_added":
            pid = ev.get("post_id")
            emoji = ev.get("emoji")
            actor = ev.get("actor") or "scott"
            p = model["posts_by_id"].get(pid)
            if p and emoji:
                actors = p["reactions"].setdefault(emoji, [])
                if actor not in actors:
                    actors.append(actor)
        elif kind == "reaction_removed":
            pid = ev.get("post_id")
            emoji = ev.get("emoji")
            actor = ev.get("actor") or "scott"
            p = model["posts_by_id"].get(pid)
            if p and emoji and emoji in p["reactions"]:
                actors = p["reactions"][emoji]
                if actor in actors:
                    actors.remove(actor)
                if not actors:
                    p["reactions"].pop(emoji, None)
        elif kind == "thread_extra_projects_set":
            ep = ev.get("extra_projects")
            if isinstance(ep, list):
                model["extra_projects"] = [str(p) for p in ep if isinstance(p, str)]
        elif kind == "thread_closed":
            model["closed_at"] = ev.get("ts")
            model["closed_by"] = ev.get("closed_by")

    model["superseded"] = sorted(model["superseded"])
    model["participants"] = seen_participants
    live_posts = [p for p in model["posts"] if not p["superseded"]]
    first_post = live_posts[0] if live_posts else None
    last_post = live_posts[-1] if live_posts else None
    model["preview"] = _preview_from(first_post["content"]) if first_post else ""
    model["post_count"] = len(live_posts)
    model["last_post_at"] = last_post["ts"] if last_post else model["started_at"]
    model["in_progress"] = model["closed_at"] is None
    return model


def summarize_thread(thread_id: str) -> dict | None:
    m = project_thread(thread_id)
    if m is None:
        return None
    return {
        "thread_id": m["thread_id"],
        "squad_id": m["squad_id"],
        "title": m.get("title", ""),
        "created_by": m["created_by"],
        "started_at": m["started_at"],
        "last_post_at": m["last_post_at"],
        "closed_at": m["closed_at"],
        "in_progress": m["in_progress"],
        "preview": m["preview"],
        "post_count": m["post_count"],
        "participants": m["participants"],
    }


def list_threads_for_squad(squad_id: str) -> list[dict]:
    out: list[dict] = []
    for tid in list_thread_ids():
        s = summarize_thread(tid)
        if s and s["squad_id"] == squad_id:
            out.append(s)
    # newest activity first
    out.sort(key=lambda x: (x["last_post_at"] or x["started_at"] or ""), reverse=True)
    return out


def render_thread_markdown(thread_id: str) -> str:
    m = project_thread(thread_id)
    if m is None:
        return ""
    squad_id = m["squad_id"] or "?"
    title = m.get("title") or ""
    heading = f"Thread {thread_id}" if not title else f"{title}"
    head = [
        f"# {heading}",
        "",
        f"**Thread**: `{thread_id}` · **Squad**: {squad_id} · **Started by**: {m['created_by']} · "
        f"**Started**: {m['started_at']}",
        "",
        "<!-- generated from events.jsonl; edits here will be overwritten -->",
        "",
        "---",
    ]
    body = []
    for p in m["posts"]:
        if p["superseded"]:
            continue
        body += [
            "",
            f"#### {p['speaker']} · {p['time']}",
            "",
            p["content"].rstrip(),
            "",
        ]
    if m["closed_at"]:
        body += ["", f"_closed by {m['closed_by']} at {m['closed_at']}_", ""]
    return "\n".join(head + body).rstrip() + "\n"


def write_thread_markdown(thread_id: str) -> Path:
    target = thread_md_path(thread_id)
    text = render_thread_markdown(thread_id)
    if not text:
        if target.exists():
            target.unlink()
        return target
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return target

