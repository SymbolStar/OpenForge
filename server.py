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
  POST   /api/threads/<id>/posts/<pid>/reactions  -> toggle {emoji, actor?}

Reads:  ~/.openclaw/openforge/squads.json,
        ~/.openclaw/openforge/threads/<thread-id>/events.jsonl
Writes: thread events (squads.json under openforge/).
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
WEB_DIR = ROOT / "web"

sys.path.insert(0, str(ROOT))
import forge_context
import forge_files
import forge_refs
import forge_store as store
import post_router

FILE_NAME_ROUTE_RE = r"([A-Za-z0-9_.\-]+\.md)"
ROOT_ID_ROUTE_RE = r"([A-Za-z0-9_\-]{1,32})"
REF_ID_ROUTE_RE = r"(ref_[A-Za-z0-9]{4,16})"
AGENT_ID_ROUTE_RE = r"([A-Za-z0-9][A-Za-z0-9._\-]{0,63})"
# v0.6 deprecated routes: keep working but emit warning headers.
DEPRECATION_DATE = "Fri, 22 May 2026 00:00:00 GMT"
SUNSET_DATE = "Wed, 01 Jul 2026 00:00:00 GMT"

SQUAD_ID_RE = re.compile(r"^\w{1,32}$")
SQUAD_ROUTE_RE = r"([\w-]{1,32})"
THREAD_ROUTE_RE = r"(th_[0-9a-f]+_[0-9a-f]+)"


def _is_local(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


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

    def _json(self, obj, status: int = 200, extra_headers: dict | None = None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _deprecated_v06_headers(self) -> dict:
        return {
            "Deprecation": DEPRECATION_DATE,
            "Sunset": SUNSET_DATE,
            "Warning": '299 - "v0.6 /api/files/<name> is deprecated; use /api/files/<root>/<name>"',
            "Link": '</api/file-roots>; rel="successor-version"',
        }

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

        # v0.7: list file roots
        if path == "/api/file-roots":
            self._json({"roots": forge_files.list_file_roots()})
            return

        # v0.7: list files in a root: /api/files?root=<id>  OR  /api/files (default)
        if path == "/api/files":
            qs = parse_qs(url.query or "")
            rid = (qs.get("root") or [None])[0]
            try:
                files = forge_files.list_files(rid)
            except forge_files.NotFoundError:
                self._json({"error": "unknown root"}, 404)
                return
            root_id = rid or forge_files.default_root().id
            self._json({"files": files, "root": root_id})
            return

        # v0.7: read with explicit root /api/files/<root>/<name>
        m = re.match(rf"^/api/files/{ROOT_ID_ROUTE_RE}/{FILE_NAME_ROUTE_RE}$", path)
        if m:
            rid, name = m.group(1), m.group(2)
            if forge_files.get_root(rid) is None:
                self._json({"error": "unknown root"}, 404)
                return
            try:
                self._json(forge_files.read_file(name, rid))
            except forge_files.FileNameError:
                self._json({"error": "invalid filename"}, 400)
            except forge_files.NotFoundError:
                self._json({"error": "not found"}, 404)
            return

        # v0.6 compat: /api/files/<name> → first root, with Deprecation header
        m = re.match(rf"^/api/files/{FILE_NAME_ROUTE_RE}$", path)
        if m:
            try:
                self._json(forge_files.read_file(m.group(1)),
                           extra_headers=self._deprecated_v06_headers())
            except forge_files.FileNameError:
                self._json({"error": "invalid filename"}, 400)
            except forge_files.NotFoundError:
                self._json({"error": "not found"}, 404)
            return
        # invalid filename inside /api/files/<...> → 400 (not 404) per PRD
        if path.startswith("/api/files/"):
            self._json({"error": "invalid filename"}, 400)
            return

        # ─── v0.8 refs ────────────────────────────────────────
        m = re.match(rf"^/api/refs/{REF_ID_ROUTE_RE}/content$", path)
        if m:
            self._refs_get_content(m.group(1))
            return
        m = re.match(rf"^/api/refs/{REF_ID_ROUTE_RE}$", path)
        if m:
            ref = forge_refs.get_ref(m.group(1))
            if not ref:
                self._json({"error": "not found"}, 404)
                return
            self._json(ref)
            return
        if path == "/api/refs":
            qs = parse_qs(url.query or "")
            agent = (qs.get("agent") or [None])[0]
            thread = (qs.get("thread") or [None])[0]
            squad = (qs.get("squad") or [None])[0]
            try:
                refs = forge_refs.list_refs(agent=agent, thread=thread, squad=squad)
            except forge_refs.RefValidationError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json({"refs": refs})
            return

        # v0.9: GET /api/agents/<id>/status
        m = re.match(rf"^/api/agents/{AGENT_ID_ROUTE_RE}/status$", path)
        if m:
            try:
                info = forge_context.read_status(m.group(1))
            except forge_context.StatusError as e:
                self._json({"error": str(e)}, 400)
                return
            if info is None:
                self._json({"error": "not found", "agent": m.group(1)}, 404)
                return
            self._json(info)
            return

        # v0.9: GET /api/agents/<id>/context-bundle?refresh=1&query=...
        m = re.match(rf"^/api/agents/{AGENT_ID_ROUTE_RE}/context-bundle$", path)
        if m:
            qs = parse_qs(url.query or "")
            refresh = (qs.get("refresh") or [""])[0] in ("1", "true", "yes")
            query_hint = (qs.get("query") or [None])[0]
            try:
                bundle = forge_context.build_context_bundle(
                    m.group(1), query_hint=query_hint, force_refresh=refresh,
                )
            except forge_context.StatusError as e:
                self._json({"error": str(e)}, 400)
                return
            d = bundle.to_dict()
            d["rendered"] = bundle.render()
            self._json(d)
            return

        self.send_error(404)

    def _refs_get_content(self, ref_id: str) -> None:
        try:
            data, mime, ref = forge_refs.read_content(ref_id)
        except forge_refs.RefNotFoundError:
            self._json({"error": "not found"}, 404)
            return
        except forge_refs.RefMissingError:
            self._json({"error": "file gone"}, 404)
            return
        except forge_refs.RefTooLargeError:
            self._json({"error": "file too large"}, 413)
            return
        except forge_refs.RefBlockedError as e:
            self._json({"error": str(e) or "blocked"}, 403)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Ref-Label", ref.get("label", ""))
        self.send_header("X-Ref-Source-Agent", ref.get("source_agent", ""))
        self.send_header("X-Ref-Id", ref.get("id", ""))
        self.end_headers()
        self.wfile.write(data)

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

        # v0.7: create in a specific root: POST /api/files/<root>
        m = re.match(rf"^/api/files/{ROOT_ID_ROUTE_RE}$", url.path)
        if m:
            rid = m.group(1)
            if forge_files.get_root(rid) is None:
                self._json({"error": "unknown root"}, 404)
                return
            opts = self._read_json()
            if opts is None:
                return
            name = (opts.get("name") or "").strip()
            content = opts.get("content") if opts.get("content") is not None else ""
            try:
                meta = forge_files.create_file(name, content, rid)
            except forge_files.FileNameError:
                self._json({"error": "invalid filename"}, 400)
                return
            except forge_files.ReadOnlyError:
                self._json({"error": "root is read-only"}, 403)
                return
            except forge_files.AlreadyExistsError:
                self._json({"error": "already exists"}, 409)
                return
            self._json(meta, 201)
            return

        # v0.8 refs: POST /api/refs (register)
        if url.path == "/api/refs":
            opts = self._read_json()
            if opts is None:
                return
            if not isinstance(opts, dict):
                self._json({"error": "body must be object"}, 400)
                return
            try:
                ref = forge_refs.register(
                    label=opts.get("label"),
                    abs_path=opts.get("abs_path"),
                    source_agent=opts.get("source_agent"),
                    thread_id=opts.get("thread_id"),
                    squad_id=opts.get("squad_id"),
                    writable=bool(opts.get("writable", False)),
                    content_type=opts.get("content_type"),
                )
            except forge_refs.RefBlockedError as e:
                self._json({"error": str(e) or "blocked"}, 403)
                return
            except forge_refs.RefTooLargeError as e:
                self._json({"error": str(e) or "too large"}, 413)
                return
            except forge_refs.RefValidationError as e:
                self._json({"error": str(e) or "invalid"}, 400)
                return
            self._json(ref, 201)
            return

        # v0.6 compat: POST /api/files → first root (with Deprecation header)
        if url.path == "/api/files":
            opts = self._read_json()
            if opts is None:
                return
            name = (opts.get("name") or "").strip()
            content = opts.get("content") if opts.get("content") is not None else ""
            try:
                meta = forge_files.create_file(name, content)
            except forge_files.FileNameError:
                self._json({"error": "invalid filename"}, 400)
                return
            except forge_files.ReadOnlyError:
                self._json({"error": "root is read-only"}, 403)
                return
            except forge_files.AlreadyExistsError:
                self._json({"error": "already exists"}, 409)
                return
            self._json(meta, 201, extra_headers=self._deprecated_v06_headers())
            return

        # v0.9: POST /api/agents/<id>/status — full replace STATUS.md
        m = re.match(rf"^/api/agents/{AGENT_ID_ROUTE_RE}/status$", url.path)
        if m:
            opts = self._read_json()
            if opts is None:
                return
            if not isinstance(opts, dict):
                self._json({"error": "body must be object"}, 400)
                return
            content = opts.get("content")
            if not isinstance(content, str):
                self._json({"error": "content (string) required"}, 400)
                return
            try:
                info = forge_context.write_status(m.group(1), content)
            except forge_context.StatusError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json(info)
            return

        self.send_error(404)

    def do_PUT(self):
        url = urlparse(self.path)
        if not self._check_auth():
            self.send_error(401, "auth required for non-local host")
            return
        # v0.8: PUT /api/refs/<id>/content
        m = re.match(rf"^/api/refs/{REF_ID_ROUTE_RE}/content$", url.path)
        if m:
            length = int(self.headers.get("Content-Length") or 0)
            if length > forge_refs.MAX_BYTES:
                self._json({"error": "body too large"}, 413)
                return
            body = self.rfile.read(length) if length else b""
            try:
                out = forge_refs.write_content(m.group(1), body)
            except forge_refs.RefNotFoundError:
                self._json({"error": "not found"}, 404)
                return
            except forge_refs.RefMissingError:
                self._json({"error": "file gone"}, 404)
                return
            except forge_refs.RefReadOnlyError:
                self._json({"error": "ref is read-only"}, 403)
                return
            except forge_refs.RefBlockedError as e:
                self._json({"error": str(e) or "blocked"}, 403)
                return
            except forge_refs.RefTooLargeError:
                self._json({"error": "body too large"}, 413)
                return
            except forge_refs.RefValidationError as e:
                self._json({"error": str(e) or "invalid"}, 400)
                return
            self._json(out)
            return
        # v0.7: PUT /api/files/<root>/<name>
        m = re.match(rf"^/api/files/{ROOT_ID_ROUTE_RE}/{FILE_NAME_ROUTE_RE}$", url.path)
        if m:
            rid, name = m.group(1), m.group(2)
            if forge_files.get_root(rid) is None:
                self._json({"error": "unknown root"}, 404)
                return
            opts = self._read_json()
            if opts is None:
                return
            content = opts.get("content")
            if content is None:
                self._json({"error": "content required"}, 400)
                return
            try:
                meta = forge_files.update_file(name, content, rid)
            except forge_files.FileNameError:
                self._json({"error": "invalid filename"}, 400)
                return
            except forge_files.ReadOnlyError:
                self._json({"error": "root is read-only"}, 403)
                return
            except forge_files.NotFoundError:
                self._json({"error": "not found"}, 404)
                return
            self._json(meta)
            return
        # v0.6 compat: PUT /api/files/<name> → first root, with Deprecation
        m = re.match(rf"^/api/files/{FILE_NAME_ROUTE_RE}$", url.path)
        if m:
            opts = self._read_json()
            if opts is None:
                return
            content = opts.get("content")
            if content is None:
                self._json({"error": "content required"}, 400)
                return
            try:
                meta = forge_files.update_file(m.group(1), content)
            except forge_files.FileNameError:
                self._json({"error": "invalid filename"}, 400)
                return
            except forge_files.ReadOnlyError:
                self._json({"error": "root is read-only"}, 403)
                return
            except forge_files.NotFoundError:
                self._json({"error": "not found"}, 404)
                return
            self._json(meta, extra_headers=self._deprecated_v06_headers())
            return
        if url.path.startswith("/api/files/"):
            self._json({"error": "invalid filename"}, 400)
            return
        self.send_error(404)

    def do_PATCH(self):
        url = urlparse(self.path)
        if not self._check_auth():
            self.send_error(401, "auth required for non-local host")
            return
        # v0.9: PATCH /api/agents/<id>/status — update one section
        m = re.match(rf"^/api/agents/{AGENT_ID_ROUTE_RE}/status$", url.path)
        if m:
            opts = self._read_json()
            if opts is None:
                return
            if not isinstance(opts, dict):
                self._json({"error": "body must be object"}, 400)
                return
            section = opts.get("section")
            content = opts.get("content")
            if not isinstance(section, str) or not section.strip():
                self._json({"error": "section required"}, 400)
                return
            if not isinstance(content, str):
                self._json({"error": "content (string) required"}, 400)
                return
            try:
                info = forge_context.patch_status_section(m.group(1), section, content)
            except forge_context.StatusError as e:
                # Distinguish 404 (section not found / no STATUS yet) from 400.
                msg = str(e)
                if "not found" in msg or "does not exist" in msg:
                    self._json({"error": msg}, 404)
                else:
                    self._json({"error": msg}, 400)
                return
            self._json(info)
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

        # v0.8: DELETE /api/refs/<id>
        m = re.match(rf"^/api/refs/{REF_ID_ROUTE_RE}$", url.path)
        if m:
            if not forge_refs.unregister(m.group(1)):
                self._json({"error": "not found"}, 404)
                return
            self.send_response(204)
            self.end_headers()
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

    print("🔨 OpenForge")
    print(f"📁 forge root:   {store.FORGE_DIR}")
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
