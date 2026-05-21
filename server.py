#!/usr/bin/env python3
"""
OpenForge server v0.4 — JSONL-backed multi-agent topic tracker.

Product (v0.4): every thread is a Slack-style topic; @mention assigns the next
agent (routing not yet wired; posts land as-is and are visible in the UI).

Routes:
  GET    /                                -> index.html
  GET    /style.css                       -> static
  GET    /app.js                          -> static

  GET    /api/squads                      -> [squads]
  POST   /api/squads                      -> create squad
  GET    /api/squads/<id>                 -> { squad, threads }
  DELETE /api/squads/<id>                 -> delete squad
  POST   /api/squads/<id>/threads         -> create thread + opening post
                                              body: { content, created_by? }

  GET    /api/threads/<id>                -> thread detail (posts)
  POST   /api/threads/<id>/posts          -> append post
                                              body: { content, speaker? }
  POST   /api/threads/<id>/close          -> mark closed

  (legacy, read-only — still serves old standup archives)
  GET    /api/standups                    -> [old standup summaries]
  GET    /api/standup/<date>              -> projected meeting model

Reads:  ~/.openclaw/openforge/squads.json,
        ~/.openclaw/openforge/threads/<thread-id>/events.jsonl,
        ~/.openclaw/standups/data/<date>/events.jsonl (legacy)
Writes: thread events (squads.json moves to openforge/).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).parent
WEB_DIR = ROOT / "web"
RUN_SCRIPT = ROOT / "run_standup.py"  # legacy, kept for CLI use only

sys.path.insert(0, str(ROOT))
import forge_store as store
import post_router

SQUAD_ID_RE = re.compile(r"^\w{1,32}$")
SQUAD_ROUTE_RE = r"([\w-]{1,32})"
THREAD_ROUTE_RE = r"(th_[0-9a-f]+_[0-9a-f]+)"


def _is_local(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


def _serializable_meeting(m: dict) -> dict:
    """Strip projection internals before sending to the UI."""
    sections = []
    for t in m["topics"]:
        sections.append({
            "id": t["id"],
            "title": t["title"],
            "kind": t["kind"],
            "idx": t["idx"],
            "posts": [
                {
                    "id": p["id"],
                    "speaker": p["speaker"],
                    "time": p["time"],
                    "ts": p["ts"],
                    "content": p["content"],
                    "mentions": p["mentions"],
                    "parent_post_id": p.get("parent_post_id"),
                    "superseded": p["superseded"],
                    "superseded_by": p.get("superseded_by"),
                }
                for p in t["posts"]
            ],
        })
    return {
        "date": m["date"],
        "title": m["title"],
        "chair": m["chair"],
        "members": m["members"],
        "started_at": m["started_at"],
        "ended_at": m["ended_at"],
        "in_progress": m["ended_at"] is None,
        "sections": sections,
    }


def _meetings_for_squad(squad_id: str) -> list[dict]:
    # legacy: only the default squad ever had standups under the date layout
    if squad_id != store.DEFAULT_SQUAD_ID:
        return []
    return list(store.iter_summaries())


def _serializable_thread(m: dict) -> dict:
    return {
        "thread_id": m["thread_id"],
        "squad_id": m["squad_id"],
        "created_by": m["created_by"],
        "started_at": m["started_at"],
        "last_post_at": m["last_post_at"],
        "closed_at": m["closed_at"],
        "closed_by": m["closed_by"],
        "in_progress": m["in_progress"],
        "preview": m["preview"],
        "post_count": m["post_count"],
        "participants": m["participants"],
        "posts": [
            {
                "id": p["id"],
                "ts": p["ts"],
                "time": p["time"],
                "speaker": p["speaker"],
                "content": p["content"],
                "mentions": p["mentions"],
                "parent_post_id": p.get("parent_post_id"),
                "superseded": p["superseded"],
                "superseded_by": p.get("superseded_by"),
                "reactions": p.get("reactions") or {},
            }
            for p in m["posts"]
        ],
    }


def _validate_squad_payload(payload: dict) -> tuple[dict | None, str | None]:
    squad_id = payload.get("id")
    members = payload.get("members")
    if not isinstance(squad_id, str) or not SQUAD_ID_RE.fullmatch(squad_id):
        return None, "id must match \\w{1,32}"
    if not isinstance(members, list) or not members:
        return None, "members must contain at least one member"
    clean_members = []
    for member in members:
        if not isinstance(member, str) or not member.strip():
            return None, "members must be non-empty strings"
        clean_members.append(member.strip())
    chair = payload.get("chair") or clean_members[0]
    if not isinstance(chair, str) or chair not in clean_members:
        return None, "chair must be one of members"
    clean = {
        "id": squad_id,
        "name": str(payload.get("name") or squad_id).strip(),
        "description": str(payload.get("description") or "").strip(),
        "emoji": str(payload.get("emoji") or "#").strip()[:8] or "#",
        "members": clean_members,
        "chair": chair,
    }
    return clean, None


def _start_standup_for_date(date: str) -> tuple[dict, int]:
    if not store.is_valid_date(date):
        return {"error": "bad date"}, 400
    if store.is_locked_exclusive(date):
        return {
            "started": False,
            "error": "already running for this date",
            "date": date,
        }, 409
    try:
        subprocess.Popen(
            [sys.executable, "-u", str(RUN_SCRIPT), "--date", date],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(ROOT),
        )
        return {"started": True, "date": date}, 200
    except Exception as e:
        return {"started": False, "error": str(e)}, 500


# ─── HTTP handler ─────────────────────────────────────────────────────
class OpenForgeHandler(BaseHTTPRequestHandler):
    server_version = "OpenForge/0.4"
    auth_token: str | None = None  # populated by main()
    bind_host: str = "127.0.0.1"

    def log_message(self, format, *args):
        pass

    # ─── helpers ──────────────────────────────────────────────────
    def _check_auth(self) -> bool:
        """Required when binding to a non-loopback host."""
        if _is_local(self.bind_host) and self.auth_token is None:
            return True
        token = self.headers.get("Authorization", "")
        token = token.removeprefix("Bearer ").strip()
        if self.auth_token and token == self.auth_token:
            return True
        # EventSource cannot send custom headers; allow ?token= for SSE.
        try:
            qs = parse_qs(urlparse(self.path).query or "")
            qtok = (qs.get("token") or [""])[0]
            return bool(self.auth_token) and qtok == self.auth_token
        except Exception:
            return False

    # ─── SSE ───────────────────────────────────────────
    def _sse_stream(self, thread_id: str) -> None:
        """Long-lived text/event-stream of thread events."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        q = store.subscribe_thread(thread_id)
        try:
            self._sse_send_raw(
                f"event: hello\ndata: {{\"thread_id\":\"{thread_id}\"}}\n\n"
            )
            last_keepalive = time.time()
            while True:
                try:
                    ev = q.get(timeout=5.0)
                except Empty:
                    ev = None
                if ev is not None:
                    payload = json.dumps(ev, ensure_ascii=False)
                    if not self._sse_send_raw(f"data: {payload}\n\n"):
                        return
                    last_keepalive = time.time()
                    continue
                if time.time() - last_keepalive >= 15.0:
                    if not self._sse_send_raw(":keepalive\n\n"):
                        return
                    last_keepalive = time.time()
        finally:
            try:
                store.unsubscribe_thread(thread_id, q)
            except Exception:
                pass

    def _sse_send_raw(self, frame: str) -> bool:
        try:
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
            return False

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ─── routes ───────────────────────────────────────────────────
    def _read_json(self, default=None):
        """Parse JSON body; on parse error, write a 400 response and return None.

        Pass `default={}` to return that value for empty bodies instead of failing.
        """
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        if not raw:
            return default if default is not None else {}
        try:
            return json.loads(raw)
        except Exception:
            self._json({"error": "bad json"}, 400)
            return None

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path

        if path in ("/", "/index.html"):
            self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/style.css":
            self._file(WEB_DIR / "style.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._file(WEB_DIR / "app.js",
                       "application/javascript; charset=utf-8")
            return

        # api endpoints
        if not self._check_auth():
            self.send_error(401, "auth required for non-local host")
            return

        if path == "/api/standups":
            self._json(list(store.iter_summaries()))
            return

        if path == "/api/squads":
            qs = parse_qs(url.query or "")
            include_archived = (qs.get("include_archived") or ["0"])[0] in ("1", "true", "yes")
            self._json(store.list_squads(include_archived=include_archived))
            return

        if path == "/api/agents":
            # Discoverable agents = union(squad members, anything that has
            # ~/.openclaw/agents/<id>/sessions). Used by the @-picker.
            ids: set[str] = set()
            for sq in store.list_squads():
                for m in sq.get("members") or []:
                    if m:
                        ids.add(m)
            agents_root = Path.home() / ".openclaw" / "agents"
            if agents_root.exists():
                for child in agents_root.iterdir():
                    if child.is_dir() and (child / "sessions").exists():
                        ids.add(child.name)
            self._json(sorted(ids))
            return

        m = re.match(rf"^/api/squads/{SQUAD_ROUTE_RE}$", path)
        if m:
            squad_id = m.group(1)
            squad = store.get_squad(squad_id)
            if squad is None:
                self._json({"error": "not found"}, 404)
                return
            self._json({
                "squad": squad,
                "threads": store.list_threads_for_squad(squad_id),
                "meetings": _meetings_for_squad(squad_id),  # legacy
            })
            return

        m = re.match(rf"^/api/threads/{THREAD_ROUTE_RE}/events$", path)
        if m:
            tid = m.group(1)
            if store.project_thread(tid) is None:
                self._json({"error": "not found"}, 404)
                return
            self._sse_stream(tid)
            return

        m = re.match(rf"^/api/threads/{THREAD_ROUTE_RE}$", path)
        if m:
            tid = m.group(1)
            data = store.project_thread(tid)
            if data is None:
                self._json({"error": "not found"}, 404)
                return
            self._json(_serializable_thread(data))
            return

        m = re.match(r"^/api/standup/(\d{4}-\d{2}-\d{2})$", path)
        if m:
            date = m.group(1)
            if not store.is_valid_date(date):
                self._json({"error": "bad date"}, 400)
                return
            data = store.project_meeting(date)
            if data is None:
                self._json({"error": "not found"}, 404)
            else:
                self._json(_serializable_meeting(data))
            return

        self.send_error(404)

    def do_POST(self):
        url = urlparse(self.path)
        if not self._check_auth():
            self.send_error(401, "auth required for non-local host")
            return

        if url.path == "/api/squads":
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                raw = json.loads(payload) if payload else {}
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            data, error = _validate_squad_payload(raw)
            if error:
                self._json({"error": error}, 400)
                return
            try:
                self._json(store.create_squad(data), 201)
            except ValueError as e:
                self._json({"error": str(e)}, 409)
            return

        m = re.match(rf"^/api/squads/{SQUAD_ROUTE_RE}/threads$", url.path)
        if m:
            squad_id = m.group(1)
            if not store.get_squad(squad_id):
                self._json({"error": "unknown squad"}, 404)
                return
            opts = self._read_json()
            if opts is None:
                return
            content = (opts.get("content") or "").strip()
            if not content:
                self._json({"error": "content required"}, 400)
                return
            created_by = (opts.get("created_by") or "scott").strip() or "scott"
            try:
                thread = store.create_thread(squad_id, created_by, content)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            # P0 post routing: if the opening post mentions agents and
            # speaker is scott, queue async fan-out.
            opening = (thread.get("posts") or [None])[0]
            if opening:
                try:
                    opening.setdefault("post_id", opening.get("id"))
                    post_router.enqueue_if_needed(thread["thread_id"], opening)
                except Exception as e:
                    print(f"⚠️  router enqueue failed: {e!r}", flush=True)
            self._json(_serializable_thread(thread), 201)
            return

        m = re.match(rf"^/api/threads/{THREAD_ROUTE_RE}/posts$", url.path)
        if m:
            tid = m.group(1)
            if store.project_thread(tid) is None:
                self._json({"error": "unknown thread"}, 404)
                return
            opts = self._read_json()
            if opts is None:
                return
            content = (opts.get("content") or "").strip()
            if not content:
                self._json({"error": "content required"}, 400)
                return
            speaker = (opts.get("speaker") or "scott").strip() or "scott"
            parent_post_id = opts.get("parent_post_id") or None
            if parent_post_id is not None and not isinstance(parent_post_id, str):
                self._json({"error": "parent_post_id must be string"}, 400)
                return
            try:
                added = store.add_thread_post(
                    tid, speaker, content, parent_post_id=parent_post_id,
                )
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            # P0 post routing
            try:
                refreshed = store.project_thread(tid) or {}
                added_id = added.get("post_id")
                post = next(
                    (p for p in (refreshed.get("posts") or [])
                     if (p.get("id") or p.get("post_id")) == added_id),
                    None,
                )
                if post:
                    # router expects `post_id`; project_thread uses `id`
                    post.setdefault("post_id", post.get("id"))
                    post_router.enqueue_if_needed(tid, post)
            except Exception as e:
                print(f"⚠️  router enqueue failed: {e!r}", flush=True)
            self._json(_serializable_thread(store.project_thread(tid)), 201)
            return

        m = re.match(rf"^/api/threads/{THREAD_ROUTE_RE}/posts/(p_[A-Za-z0-9_]+)/reactions$", url.path)
        if m:
            tid = m.group(1)
            pid = m.group(2)
            if store.project_thread(tid) is None:
                self._json({"error": "unknown thread"}, 404)
                return
            opts = self._read_json()
            if opts is None:
                return
            emoji = (opts.get("emoji") or "").strip()
            actor = (opts.get("actor") or "scott").strip() or "scott"
            if not emoji:
                self._json({"error": "emoji required"}, 400)
                return
            try:
                reactions = store.toggle_reaction(tid, pid, emoji, actor)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json({"post_id": pid, "reactions": reactions})
            return

        m = re.match(rf"^/api/threads/{THREAD_ROUTE_RE}/close$", url.path)
        if m:
            tid = m.group(1)
            if store.project_thread(tid) is None:
                self._json({"error": "unknown thread"}, 404)
                return
            opts = self._read_json(default={}) or {}
            store.close_thread(tid, (opts.get("closed_by") or "scott").strip() or "scott")
            self._json(_serializable_thread(store.project_thread(tid)))
            return

        m = re.match(rf"^/api/squads/{SQUAD_ROUTE_RE}/run$", url.path)
        if m:
            payload, status = _start_standup_for_date(datetime.now().strftime("%Y-%m-%d"))
            self._json(payload, status)
            return

        if url.path == "/api/run":
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                opts = json.loads(payload) if payload else {}
            except Exception:
                self._json({"error": "bad json"}, 400)
                return

            date = opts.get("date") or datetime.now().strftime("%Y-%m-%d")
            if not store.is_valid_date(date):
                self._json({"error": "bad date"}, 400)
                return
            payload, status = _start_standup_for_date(date)
            self._json(payload, status)
            return

        self.send_error(404)

    def do_PATCH(self):
        url = urlparse(self.path)
        if not self._check_auth():
            self.send_error(401, "auth required for non-local host")
            return
        m = re.match(rf"^/api/squads/{SQUAD_ROUTE_RE}$", url.path)
        if not m:
            self.send_error(404)
            return
        squad_id = m.group(1)
        opts = self._read_json()
        if opts is None:
            return
        try:
            updated = store.update_squad(squad_id, opts)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
            return
        if updated is None:
            self._json({"error": "not found"}, 404)
            return
        self._json(updated)

    def do_DELETE(self):
        url = urlparse(self.path)
        if not self._check_auth():
            self.send_error(401, "auth required for non-local host")
            return

        m = re.match(rf"^/api/squads/{SQUAD_ROUTE_RE}$", url.path)
        if not m:
            self.send_error(404)
            return
        squad_id = m.group(1)
        if not store.delete_squad(squad_id):
            self._json({"error": "not found"}, 404)
            return
        self._json({"deleted": True, "id": squad_id})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=7878)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument(
        "--token",
        help="Bearer token required for API access. "
             "Auto-generated when --host is non-loopback.",
    )
    args = p.parse_args()

    OpenForgeHandler.bind_host = args.host
    if not _is_local(args.host):
        OpenForgeHandler.auth_token = args.token or secrets.token_urlsafe(24)
        print(f"🔐 auth token: {OpenForgeHandler.auth_token}")
    elif args.token:
        OpenForgeHandler.auth_token = args.token

    print(f"🔨 OpenForge")
    print(f"📁 forge root:   {store.FORGE_DIR}")
    print(f"📝 legacy root:  {store.STANDUP_DIR}")
    # heal any agent main pointers a previous run left polluted.
    try:
        all_members: set[str] = set()
        for sq in store.list_squads():
            for m in sq.get("members") or []:
                if m:
                    all_members.add(m)
        # also include every agent that has an on-disk session dir
        agents_root = Path.home() / ".openclaw" / "agents"
        if agents_root.exists():
            for child in agents_root.iterdir():
                if child.is_dir() and (child / "sessions" / "sessions.json").exists():
                    all_members.add(child.name)
        healed = post_router.heal_polluted_mains(sorted(all_members))
        if healed:
            print(f"🩹 healed polluted main session for: {', '.join(healed)}")
    except Exception as e:
        print(f"⚠️  heal step failed: {e!r}")
    print(f"🌐 server:        http://{args.host}:{args.port}")
    server = ThreadingHTTPServer((args.host, args.port), OpenForgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye 🦞")


if __name__ == "__main__":
    main()
