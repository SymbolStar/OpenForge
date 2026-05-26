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
import base64
import binascii
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
import forge_config
import forge_context
import forge_employees
import forge_files
import forge_identity
import forge_refs
import forge_session_search
import forge_store as store
import forge_uploads
import post_router

FILE_NAME_ROUTE_RE = r"([A-Za-z0-9_.\-]+\.md)"
ROOT_ID_ROUTE_RE = r"([A-Za-z0-9_\-]{1,32})"

# ─────────────────────────────────────────────────────────────────────
# OPERATOR_ID — the single human who is allowed to close a thread.
#
# V1.0.0 conscious limitation (see docs/AGENT-COLLAB-OPEN-QUESTIONS.md Q2):
# OpenForge is a local cockpit for one human ("scott"). Close permission is
# hard-coded here rather than derived from auth/identity, because:
#   - There is no multi-user auth story yet.
#   - PRD-v1.0 Rule 6 explicitly says "only Scott closes".
# The day a second human operator shows up — or we expose OpenForge beyond
# loopback for real — this constant is the FIRST place to change. Replace
# the equality check with an identity-aware lookup (bearer token → operator
# id, or role tag on the speaker) and the close handler will be the only
# site that needs to follow.
# ─────────────────────────────────────────────────────────────────────
OPERATOR_ID = "scott"
REF_ID_ROUTE_RE = r"(ref_[A-Za-z0-9]{4,16})"
AGENT_ID_ROUTE_RE = r"([A-Za-z0-9][A-Za-z0-9._\-]{0,63})"
# v0.6 deprecated routes: keep working but emit warning headers.
DEPRECATION_DATE = "Fri, 22 May 2026 00:00:00 GMT"
SUNSET_DATE = "Wed, 01 Jul 2026 00:00:00 GMT"

SQUAD_ID_RE = re.compile(r"^[A-Za-z0-9][\w-]{0,31}$")
SQUAD_ROUTE_RE = r"([\w-]{1,32})"
THREAD_ROUTE_RE = r"(th_[0-9a-f]+_[0-9a-f]+)"


def _is_local(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


def _serializable_thread(m: dict) -> dict:
    return {
        "thread_id": m["thread_id"],
        "squad_id": m["squad_id"],
        "title": m.get("title", ""),
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
        return None, "id 必须以字母/数字开头，仅允许 [A-Za-z0-9_-]，最长 32 字符"
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
    # v0.5 PR-A: project_dir is optional. Empty string / None / missing all
    # collapse to None ("discussion squad"). If provided, must be a string
    # AND an absolute path. Existence / git-repo checks are runtime concerns
    # (see /api/fs/validate); we do NOT block writes on them.
    project_dir: str | None = None
    if "project_dir" in payload:
        raw_pd = payload.get("project_dir")
        if raw_pd is None or raw_pd == "":
            project_dir = None
        elif not isinstance(raw_pd, str):
            return None, "project_dir must be a string or null"
        else:
            pd = raw_pd.strip()
            if not pd:
                project_dir = None
            elif not pd.startswith("/"):
                return None, "project_dir must be an absolute path"
            else:
                project_dir = pd
    clean = {
        "id": squad_id,
        "name": str(payload.get("name") or squad_id).strip(),
        "description": str(payload.get("description") or "").strip(),
        "emoji": str(payload.get("emoji") or "#").strip()[:8] or "#",
        "members": clean_members,
        "chair": chair,
        "project_dir": project_dir,
    }
    return clean, None


# ─── /api/fs/validate cache (PR-A → PR-B1: now lives in forge_project) ─
# project_dir validation hits the filesystem (stat + .git lookup). Squads
# list views and individual squad GETs need the `project_dir_valid` derived
# field on every render — without a cache, each render does N stats. PR-B1
# extracted the cache + helpers to forge_project so post_router can share
# them when deciding whether to inject the `[project]` segment.
import forge_project


def _fs_validate_path(path: str) -> dict:
    return forge_project.validate(path)


def _fs_validate_invalidate(*paths: str | None) -> None:
    forge_project.invalidate(*paths)


def _squad_with_validity(squad: dict | None) -> dict | None:
    """Add derived project_dir_valid: bool | None to a squad dict.

    - None  → field not configured
    - True  → path exists AND is a git repo
    - False → configured but exists/git check failed
    """
    if squad is None:
        return None
    out = dict(squad)
    out["project_dir_valid"] = forge_project.derive_validity(out.get("project_dir"))
    return out


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
    # ─── speaker spoofing guard ────────────────────────────────────
    # The browser cockpit is the only trusted caller allowed to post as
    # scott. Agents and stray scripts can hit the loopback API too, so we
    # require an explicit speaker on every post and refuse `scott` unless
    # the caller proves it's the UI (sends `X-OpenForge-UI: 1`, which
    # web/app.js adds to every POST and which no agent prompt teaches).
    #
    # Before this guard a missing/empty speaker silently defaulted to
    # "scott", which let any sub-agent that curled the loopback API
    # impersonate the CEO in a thread (real incident 2026-05-25: designer
    # posted a "forge pipeline" summary as scott). See AGENT-THREAD-
    # COLLABORATION.md §4.1 for the envelope-vs-content discussion.
    def _resolve_speaker(self, opts: dict, *, field: str = "speaker"):
        """Validate and return the caller-claimed speaker, or None on error.

        On error the JSON response is already written.
        """
        raw = opts.get(field)
        speaker = (raw or "").strip() if isinstance(raw, str) else ""
        if not speaker:
            self._json(
                {"error": f"{field} required (no default; UI sends 'scott',"
                          f" agents must send their own agent id)"},
                400,
            )
            return None
        low = speaker.lower()
        if low == "__router__":
            self._json({"error": f"{field}='{speaker}' is reserved"}, 400)
            return None
        if low == "scott":
            ui_marker = (self.headers.get("X-OpenForge-UI") or "").strip()
            if ui_marker != "1":
                self._json(
                    {"error": "posting as 'scott' is reserved for the"
                              " OpenForge UI; agents must use their own"
                              " agent id as speaker"},
                    403,
                )
                return None
        return speaker

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
            squads = store.list_squads(include_archived=include_archived)
            squads = [_squad_with_validity(s) for s in squads]
            self._json(squads)
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

        if path == "/api/employees":
            # Curated employee roster. Returns list[str] of agent ids
            # (back-compat) OR list[{id,name,emoji}] when ?with_identity=1.
            # V1.2 (Scott 2026-05-24 21:22): the enriched form drives
            # display-name rendering in the UI; the bare-string form is
            # still consumed by older code paths (squad member picker
            # validation, isEmployee() lookups). Keeping both behind one
            # endpoint instead of forking the URL keeps the auth /
            # caching surface area unchanged.
            qs = parse_qs(url.query or "")
            if (qs.get("with_identity") or ["0"])[0] in ("1", "true", "yes"):
                self._json(forge_identity.list_identities())
            else:
                self._json(forge_employees.list_employees())
            return

        if path == "/api/config":
            # V1.0.0 §4.3: front-end pulls this at boot to learn the
            # webchat base URL (employee-avatar deep-links). Cheap GET,
            # never blocks rendering on the client side — client falls
            # back to its hardcoded default if this 5xx's.
            self._json(forge_config.get_config())
            return

        if path == "/api/fs/validate":
            # PR-A: cheap fs check used by the squad-editor blur handler.
            # Returns 200 with {exists, is_git_repo, error} for any absolute
            # path; 400 only for malformed input. Cached 60s server-side.
            qs = parse_qs(url.query or "")
            raw = (qs.get("path") or [""])[0]
            if not raw:
                self._json({"error": "path is required"}, 400)
                return
            if not raw.startswith("/"):
                self._json({"error": "path must be absolute"}, 400)
                return
            self._json(_fs_validate_path(raw))
            return

        m = re.match(rf"^/api/squads/{SQUAD_ROUTE_RE}$", path)
        if m:
            squad_id = m.group(1)
            squad = store.get_squad(squad_id)
            if squad is None:
                self._json({"error": "not found"}, 404)
                return
            self._json({
                "squad": _squad_with_validity(squad),
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

        # uploads (paste-image feature): GET /api/uploads/<filename>
        m = re.match(r"^/api/uploads/([A-Za-z0-9._-]+)$", path)
        if m:
            resolved = forge_uploads.get_upload_path(m.group(1))
            if resolved is None:
                self._json({"error": "not found"}, 404)
                return
            up_path, up_mime = resolved
            self._file(up_path, up_mime)
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

        # v0.9.2: GET /api/agents/<id>/session-search?q=...&days=30&max=10&scope=main
        m = re.match(rf"^/api/agents/{AGENT_ID_ROUTE_RE}/session-search$", path)
        if m:
            qs = parse_qs(url.query or "")
            q = (qs.get("q") or qs.get("query") or [""])[0]
            days_raw = (qs.get("days") or [str(forge_session_search.DEFAULT_DAYS)])[0]
            max_raw = (qs.get("max") or [str(forge_session_search.DEFAULT_MAX_HITS)])[0]
            scope = (qs.get("scope") or ["main"])[0]
            try:
                days = int(days_raw)
                max_hits = int(max_raw)
            except ValueError:
                self._json({"error": "days/max must be integers"}, 400)
                return
            try:
                result = forge_session_search.search(
                    m.group(1), q, days=days, max_hits=max_hits, scope=scope,
                )
            except forge_session_search.SessionSearchError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json(result)
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

        # Image upload (paste-image in composer): POST /api/uploads
        # Body: {"content_base64": "...", "content_type": "image/png"}
        if url.path == "/api/uploads":
            opts = self._read_json()
            if opts is None:
                return
            b64 = opts.get("content_base64")
            mime = opts.get("content_type")
            if not isinstance(b64, str) or not b64:
                self._json({"error": "content_base64 required"}, 400)
                return
            try:
                raw = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError):
                self._json({"error": "content_base64 is not valid base64"}, 400)
                return
            if len(raw) > forge_uploads.MAX_BYTES:
                self._json({"error": f"file too large: max {forge_uploads.MAX_BYTES} bytes"}, 413)
                return
            try:
                meta = forge_uploads.save_upload(raw, mime)
            except forge_uploads.UploadError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json(meta, 201)
            return

        # v0.10 paste-as-ref: POST /api/uploads/refs
        # Body: {"content_base64":"...","content_type":"image/png",
        #        "label":"paste.png","source_agent":"scott",
        #        "squad_id":"..."?, "thread_id":"..."?}
        # Writes the bytes into the operator's workspace upload dir, then
        # registers the file via forge_refs.register() and returns the ref.
        if url.path == "/api/uploads/refs":
            opts = self._read_json()
            if opts is None:
                return
            if not isinstance(opts, dict):
                self._json({"error": "body must be object"}, 400)
                return
            b64 = opts.get("content_base64")
            mime = opts.get("content_type")
            if not isinstance(b64, str) or not b64:
                self._json({"error": "content_base64 required"}, 400)
                return
            try:
                raw = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError):
                self._json({"error": "content_base64 is not valid base64"}, 400)
                return
            if len(raw) > forge_uploads.MAX_BYTES:
                self._json({"error": f"file too large: max {forge_uploads.MAX_BYTES} bytes"}, 413)
                return
            source_agent = self._resolve_speaker(opts, field="source_agent")
            if source_agent is None:
                return
            try:
                meta = forge_uploads.save_upload_to_workspace(
                    raw, mime, source_agent
                )
            except forge_uploads.UploadError as e:
                self._json({"error": str(e)}, 400)
                return
            label = (opts.get("label") or meta["filename"]).strip() or meta["filename"]
            try:
                ref = forge_refs.register(
                    label=label,
                    abs_path=meta["abs_path"],
                    source_agent=source_agent,
                    thread_id=opts.get("thread_id"),
                    squad_id=opts.get("squad_id"),
                    writable=False,
                    content_type=meta["content_type"],
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
            payload = {**ref, "upload": meta}
            self._json(payload, 201)
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
                self._json(_squad_with_validity(store.create_squad(data)), 201)
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
            # v0.10: prefer explicit `title` + optional `content`.
            # Legacy callers passing only `content` still work (title derived).
            raw_title = opts.get("title")
            content = (opts.get("content") or "").strip()
            if raw_title is None and not content:
                self._json({"error": "title or content required"}, 400)
                return
            created_by = self._resolve_speaker(opts, field="created_by")
            if created_by is None:
                return
            try:
                if raw_title is not None:
                    thread = store.create_thread(
                        squad_id, created_by,
                        title=raw_title,
                        opening_content=content or None,
                    )
                else:
                    thread = store.create_thread(squad_id, created_by, content)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            # P0 post routing: if the opening post mentions agents and
            # speaker is scott, queue async fan-out.
            posts = thread.get("posts") or []
            opening = posts[0] if posts else None
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
            speaker = self._resolve_speaker(opts)
            if speaker is None:
                return
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
            actor = self._resolve_speaker(opts, field="actor")
            if actor is None:
                return
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
            # PRD-v1.0 §3 Rule 6: only Scott closes. Accept either `by`
            # (PRD field name) or `closed_by` (legacy field). Field is
            # REQUIRED — missing → 400. Value must equal OPERATOR_ID — any
            # other id → 403.
            raw_by = opts.get("by")
            if raw_by is None:
                raw_by = opts.get("closed_by")
            if raw_by is None:
                self._json({"error": "`by` field required"}, 400)
                return
            by = str(raw_by).strip()
            if by != OPERATOR_ID:
                self._json({"error": "thread close 权限仅限 scott"}, 403)
                return
            store.close_thread(tid, by)
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
        # PR-A: pre-validate project_dir before letting it reach the store.
        # The store only checks the type; the absolute-path rule lives at
        # the API layer (same as POST / _validate_squad_payload).
        if isinstance(opts, dict) and "project_dir" in opts:
            raw_pd = opts.get("project_dir")
            if raw_pd is None or raw_pd == "":
                opts["project_dir"] = None
            elif not isinstance(raw_pd, str):
                self._json({"error": "project_dir must be a string or null"}, 400)
                return
            else:
                pd = raw_pd.strip()
                if not pd:
                    opts["project_dir"] = None
                elif not pd.startswith("/"):
                    self._json({"error": "project_dir must be an absolute path"}, 400)
                    return
                else:
                    opts["project_dir"] = pd
        try:
            old = store.get_squad(squad_id)
            updated = store.update_squad(squad_id, opts)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
            return
        if updated is None:
            self._json({"error": "not found"}, 404)
            return
        # PR-A: any change to project_dir invalidates the fs-validate cache
        # for both the old and the new path (catches typo-fix + clear).
        old_pd = (old or {}).get("project_dir")
        new_pd = updated.get("project_dir")
        if old_pd != new_pd:
            _fs_validate_invalidate(old_pd, new_pd)
        self._json(_squad_with_validity(updated))

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
        # Snapshot before delete so we can drop the fs-validate cache entry.
        prev_pd = (store.get_squad(squad_id) or {}).get("project_dir")
        if not store.delete_squad(squad_id):
            self._json({"error": "not found"}, 404)
            return
        _fs_validate_invalidate(prev_pd)
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
    # Recover orphan placeholders left dangling by a previous restart.
    # See post_router.recover_orphan_placeholders for the why; in short,
    # `_inflight` is process-local memory and `forge restart` mid-turn
    # leaves a `⏳ @X 正在思考中…` post that nothing will ever
    # supersede, silently freezing that thread. Sweep once at boot.
    try:
        recovered = post_router.recover_orphan_placeholders(redispatch=True)
        if recovered:
            redisp = sum(1 for r in recovered if r.get("redispatched"))
            print(
                f"🧩 recovered {len(recovered)} orphan placeholder(s); "
                f"re-dispatched {redisp}"
            )
    except Exception as e:
        print(f"⚠️  orphan-placeholder sweep failed: {e!r}")
    print(f"🌐 server:        http://{args.host}:{args.port}")
    server = ThreadingHTTPServer((args.host, args.port), OpenForgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye 🦞")


if __name__ == "__main__":
    main()
