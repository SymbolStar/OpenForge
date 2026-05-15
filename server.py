#!/usr/bin/env python3
"""
huddle server v0.3 — JSONL-backed Slack-like viewer.

Routes:
  GET  /                         -> index.html
  GET  /style.css                -> static
  GET  /app.js                   -> static
  GET  /api/squads               -> [squads]
  GET  /api/squads/<id>          -> squad + meetings
  POST /api/squads               -> create squad
  DELETE /api/squads/<id>        -> delete squad
  POST /api/squads/<id>/run      -> launch run_standup.py for today
  GET  /api/standups             -> [summaries]
  GET  /api/standup/<date>       -> projected meeting model
  POST /api/run                  -> launch run_standup.py for a given date
                                    (validated; refuses if already running)

Reads:  ~/.openclaw/standups/data/<date>/events.jsonl
Writes: nothing (the run_standup.py subprocess writes events + md)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent
WEB_DIR = ROOT / "web"
RUN_SCRIPT = ROOT / "run_standup.py"

sys.path.insert(0, str(ROOT))
import forge_store as store

SQUAD_ID_RE = re.compile(r"^\w{1,32}$")
SQUAD_ROUTE_RE = r"([\w-]{1,32})"


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
    # TODO: filter by squad metadata once meeting_started stores squad_id.
    if squad_id != store.DEFAULT_SQUAD_ID:
        return []
    return list(store.iter_summaries())


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
class HuddleHandler(BaseHTTPRequestHandler):
    server_version = "Huddle/0.3"
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
        return bool(self.auth_token) and token == self.auth_token

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
            self._json(store.list_squads())
            return

        m = re.match(rf"^/api/squads/{SQUAD_ROUTE_RE}$", path)
        if m:
            squad_id = m.group(1)
            squad = store.get_squad(squad_id)
            if squad is None:
                self._json({"error": "not found"}, 404)
                return
            self._json({"squad": squad, "meetings": _meetings_for_squad(squad_id)})
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
        if squad_id == store.DEFAULT_SQUAD_ID:
            self._json({"error": "cannot delete default squad"}, 400)
            return
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

    HuddleHandler.bind_host = args.host
    if not _is_local(args.host):
        HuddleHandler.auth_token = args.token or secrets.token_urlsafe(24)
        print(f"🔐 auth token: {HuddleHandler.auth_token}")
    elif args.token:
        HuddleHandler.auth_token = args.token

    print(f"📍 events root: {store.DATA_DIR}")
    print(f"📄 markdown root: {store.STANDUP_DIR}")
    print(f"🌐 huddle:        http://{args.host}:{args.port}")
    server = ThreadingHTTPServer((args.host, args.port), HuddleHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye 🦞")


if __name__ == "__main__":
    main()
