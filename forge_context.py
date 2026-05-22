"""OpenForge v0.9 — Agent Context Bundle.

Assemble three context sources for an agent's spawned (child) session so it
starts with the same situational awareness as its main session:

  1. STATUS.md  — agent-curated "what I'm doing right now"
  2. main_session — last N turns from agent:<id>:main jsonl
  3. memory — top K hits from `openclaw memory search`

All sources fail soft: any one source raising / timing out / being empty is
skipped silently, the bundle still ships.

Storage layout (under $HOME, honors monkeypatch):
  ~/.openclaw/workspace-<agent>/STATUS.md
  ~/.openclaw/openforge/config.json   (key: agents.<id>.contextBundle)
  ~/.openclaw/openforge/context_bundles/<agent>.json   (short TTL cache)

Public API:
    build_context_bundle(agent_id, query_hint=None, force_refresh=False) -> ContextBundle
    write_status(agent_id, content) -> dict
    patch_status_section(agent_id, section, content) -> dict
    read_status(agent_id) -> dict | None
    load_agent_config(agent_id) -> dict
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# ─── constants / defaults ────────────────────────────────────────────

DEFAULT_BUNDLE_CONFIG = {
    "enabled": True,
    "include": ["status", "main_session", "memory"],
    "main_session_turns": 20,
    "memory_top_k": 5,
    "status_max_bytes": 4096,
    "main_session_max_bytes": 8192,
    "memory_max_bytes": 4096,
    "cache_ttl_seconds": 60,
    "main_session_key": None,  # default: agent:<id>:main
}

MEMORY_SEARCH_TIMEOUT = float(os.environ.get("OPENFORGE_MEMORY_SEARCH_TIMEOUT", "5"))
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# ─── exceptions ──────────────────────────────────────────────────────


class StatusError(ValueError):
    """Bad agent id, payload, or write failure on STATUS.md."""


# ─── path helpers ────────────────────────────────────────────────────


def _home() -> Path:
    return Path.home()


def forge_dir() -> Path:
    p = _home() / ".openclaw" / "openforge"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _config_path() -> Path:
    return forge_dir() / "config.json"


def _bundles_dir() -> Path:
    p = forge_dir() / "context_bundles"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _workspace_dir(agent_id: str) -> Path:
    return _home() / ".openclaw" / f"workspace-{agent_id}"


def _status_path(agent_id: str) -> Path:
    return _workspace_dir(agent_id) / "STATUS.md"


def _agent_sessions_root(agent_id: str) -> Path:
    return _home() / ".openclaw" / "agents" / agent_id / "sessions"


def _validate_agent_id(agent_id) -> str:
    if not isinstance(agent_id, str) or not AGENT_ID_RE.match(agent_id):
        raise StatusError("invalid agent_id")
    return agent_id


# ─── config ──────────────────────────────────────────────────────────


def _load_raw_config() -> dict:
    p = _config_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_agent_config(agent_id: str) -> dict:
    """Return merged bundle config for a single agent.

    Order: DEFAULT_BUNDLE_CONFIG ← agents.<id>.contextBundle.
    Also resolves mainSessionKey from agents.<id>.mainSessionKey.
    """
    _validate_agent_id(agent_id)
    cfg = _load_raw_config()
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    agent_cfg = agents.get(agent_id) if isinstance(agents.get(agent_id), dict) else {}
    bundle_cfg = agent_cfg.get("contextBundle") if isinstance(agent_cfg.get("contextBundle"), dict) else {}
    merged = dict(DEFAULT_BUNDLE_CONFIG)
    merged.update({k: v for k, v in bundle_cfg.items() if v is not None})
    # mainSessionKey lives one level up (agents.<id>.mainSessionKey)
    msk = agent_cfg.get("mainSessionKey")
    if isinstance(msk, str) and msk.strip():
        merged["main_session_key"] = msk.strip()
    elif not merged.get("main_session_key"):
        merged["main_session_key"] = f"agent:{agent_id}:main"
    # Sanitize include list
    include = merged.get("include") or []
    if not isinstance(include, list):
        include = list(DEFAULT_BUNDLE_CONFIG["include"])
    merged["include"] = [str(x) for x in include if isinstance(x, str)]
    return merged


# ─── STATUS.md read/write ────────────────────────────────────────────


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _stamp_updated_line(content: str, agent_id: str) -> str:
    """Ensure the canonical `> 最后更新：...` line near the top, replacing prior.

    If the file has no markdown header, leave content as-is but prepend the stamp.
    """
    stamp_line = f"> 最后更新：{time.strftime('%Y-%m-%d %H:%M:%S')} by openforge"
    lines = content.splitlines()
    out: list[str] = []
    inserted = False
    skip_existing_stamp = False
    for line in lines:
        if not inserted and line.startswith("> 最后更新："):
            out.append(stamp_line)
            inserted = True
            skip_existing_stamp = True
            continue
        out.append(line)
    if not inserted:
        # Insert after first h1 if present, else at top.
        for i, line in enumerate(out):
            if line.startswith("# "):
                out.insert(i + 1, "")
                out.insert(i + 2, stamp_line)
                inserted = True
                break
        if not inserted:
            out = [stamp_line, ""] + out
    _ = skip_existing_stamp  # silence linter
    return "\n".join(out).rstrip() + "\n"


def write_status(agent_id: str, content: str) -> dict:
    """Replace STATUS.md atomically. Returns metadata dict."""
    _validate_agent_id(agent_id)
    if not isinstance(content, str):
        raise StatusError("content must be string")
    if len(content) > 64 * 1024:
        raise StatusError("STATUS.md too large (max 64KB)")
    final = _stamp_updated_line(content, agent_id)
    path = _status_path(agent_id)
    _atomic_write(path, final)
    # invalidate cache
    _invalidate_cache(agent_id)
    st = path.stat()
    return {
        "agent": agent_id,
        "size": st.st_size,
        "updated_at": st.st_mtime,
        "path": str(path),
    }


def read_status(agent_id: str) -> dict | None:
    """Return STATUS.md content + metadata, or None if not present."""
    _validate_agent_id(agent_id)
    path = _status_path(agent_id)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        st = path.stat()
    except OSError:
        return None
    return {
        "agent": agent_id,
        "content": content,
        "updated_at": st.st_mtime,
        "size": st.st_size,
        "path": str(path),
    }


def patch_status_section(agent_id: str, section: str, content: str) -> dict:
    """Replace the contents of one `## <section>` block.

    The section header must already exist. Raises StatusError otherwise.
    The replacement is everything between the matched `## <section>` header
    and the next `## ` header (or EOF).
    """
    _validate_agent_id(agent_id)
    if not isinstance(section, str) or not section.strip():
        raise StatusError("section required")
    if not isinstance(content, str):
        raise StatusError("content must be string")
    section = section.strip()
    cur = read_status(agent_id)
    if not cur:
        raise StatusError("STATUS.md does not exist; POST a full document first")
    body = cur["content"]
    lines = body.splitlines()
    # Find target header
    header_idx = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("## ") and s[3:].strip() == section:
            header_idx = i
            break
    if header_idx is None:
        raise StatusError(f"section not found: {section}")
    # Find end (next "## " line or EOF)
    end_idx = len(lines)
    for j in range(header_idx + 1, len(lines)):
        if lines[j].strip().startswith("## "):
            end_idx = j
            break
    new_block = [lines[header_idx], content.rstrip()]
    new_lines = lines[:header_idx] + new_block + lines[end_idx:]
    new_body = "\n".join(new_lines).rstrip() + "\n"
    return write_status(agent_id, new_body)


# ─── source: main session ────────────────────────────────────────────


def _read_sessions_index(agent_id: str) -> dict:
    p = _agent_sessions_root(agent_id) / "sessions.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _resolve_session_file(agent_id: str, main_session_key: str) -> Path | None:
    """Look up sessions.json to find sessionFile for main_session_key."""
    idx = _read_sessions_index(agent_id)
    entry = idx.get(main_session_key)
    if isinstance(entry, dict):
        f = entry.get("sessionFile")
        if isinstance(f, str) and Path(f).exists():
            return Path(f)
        sid = entry.get("sessionId")
        if isinstance(sid, str):
            candidate = _agent_sessions_root(agent_id) / f"{sid}.jsonl"
            if candidate.exists():
                return candidate
    return None


def _extract_turns_from_session(path: Path, max_turns: int) -> list[dict]:
    """Return up to max_turns recent {role, text, ts} dicts from a session jsonl."""
    if max_turns <= 0:
        return []
    turns: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "message":
                    continue
                msg = rec.get("message") or {}
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content")
                text = _flatten_message_content(content)
                if not text:
                    continue
                turns.append({
                    "role": role,
                    "text": text,
                    "ts": rec.get("timestamp") or msg.get("timestamp"),
                })
    except OSError:
        return []
    # Keep tail
    return turns[-max_turns:]


def _flatten_message_content(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for piece in content:
            if isinstance(piece, dict):
                t = piece.get("text") or piece.get("content")
                if isinstance(t, str):
                    chunks.append(t)
            elif isinstance(piece, str):
                chunks.append(piece)
        return "\n".join(c for c in (s.strip() for s in chunks) if c)
    return ""


def collect_main_session(agent_id: str, cfg: dict) -> dict:
    """Returns {"turns": [...], "session_key": str, "truncated": bool} or {}."""
    try:
        max_turns = int(cfg.get("main_session_turns") or 0)
        max_bytes = int(cfg.get("main_session_max_bytes") or 0)
        key = cfg.get("main_session_key") or f"agent:{agent_id}:main"
        sf = _resolve_session_file(agent_id, key)
        if not sf:
            return {}
        turns = _extract_turns_from_session(sf, max_turns)
        if not turns:
            return {}
        # Enforce byte budget by trimming oldest turns.
        truncated = False
        if max_bytes > 0:
            total = sum(len(t["text"].encode("utf-8")) for t in turns)
            while turns and total > max_bytes:
                drop = turns.pop(0)
                total -= len(drop["text"].encode("utf-8"))
                truncated = True
        return {
            "session_key": key,
            "session_file": str(sf),
            "turns": turns,
            "truncated": truncated,
        }
    except Exception:
        return {}


# ─── source: STATUS ───────────────────────────────────────────────────


def collect_status(agent_id: str, cfg: dict) -> dict:
    """Returns {"content": str, "updated_at": float, "truncated": bool} or {}."""
    info = read_status(agent_id)
    if not info:
        return {}
    content = info["content"]
    max_bytes = int(cfg.get("status_max_bytes") or 0)
    truncated = False
    if max_bytes > 0 and len(content.encode("utf-8")) > max_bytes:
        # Trim from the end on a UTF-8 boundary.
        b = content.encode("utf-8")[:max_bytes]
        # Drop any trailing partial multibyte char.
        while b and (b[-1] & 0xC0) == 0x80:
            b = b[:-1]
        content = b.decode("utf-8", errors="ignore") + "\n…[truncated]"
        truncated = True
    return {
        "content": content,
        "updated_at": info["updated_at"],
        "truncated": truncated,
    }


# ─── source: memory_search ───────────────────────────────────────────


def _openclaw_bin() -> str:
    override = os.environ.get("OPENFORGE_OPENCLAW_BIN")
    if override and Path(override).exists():
        return override
    return "openclaw"


def collect_memory(agent_id: str, cfg: dict, query_hint: str | None) -> dict:
    """Run `openclaw memory search` and return {"hits": [...]} or {}."""
    if not query_hint or not query_hint.strip():
        return {}
    top_k = int(cfg.get("memory_top_k") or 0)
    max_bytes = int(cfg.get("memory_max_bytes") or 0)
    if top_k <= 0:
        return {}
    bin_path = _openclaw_bin()
    try:
        result = subprocess.run(
            [
                bin_path, "memory", "search",
                "--agent", agent_id,
                "--json",
                "--max-results", str(top_k),
                "--query", query_hint[:500],
            ],
            capture_output=True,
            text=True,
            timeout=MEMORY_SEARCH_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}
    if result.returncode != 0:
        return {}
    raw = (result.stdout or "").strip()
    if not raw:
        return {}
    try:
        blob = json.loads(raw)
    except Exception:
        return {}
    hits = _normalize_memory_hits(blob)
    if not hits:
        return {}
    hits = hits[:top_k]
    # Apply byte budget.
    truncated = False
    if max_bytes > 0:
        total = 0
        kept: list[dict] = []
        for h in hits:
            snippet = h.get("snippet") or ""
            cost = len(snippet.encode("utf-8")) + 64
            if total + cost > max_bytes and kept:
                truncated = True
                break
            kept.append(h)
            total += cost
        hits = kept
    return {"hits": hits, "truncated": truncated, "query": query_hint}


def _normalize_memory_hits(blob) -> list[dict]:
    """Coerce `openclaw memory search --json` output into list[{path,snippet,score}]."""
    candidates = []
    if isinstance(blob, list):
        candidates = blob
    elif isinstance(blob, dict):
        for key in ("hits", "results", "matches", "data"):
            if isinstance(blob.get(key), list):
                candidates = blob[key]
                break
    out: list[dict] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        out.append({
            "path": str(c.get("path") or c.get("file") or c.get("source") or ""),
            "snippet": str(c.get("snippet") or c.get("text") or c.get("content") or "")[:1000],
            "score": float(c.get("score") or 0.0) if isinstance(c.get("score"), (int, float)) else 0.0,
        })
    return out


# ─── bundle assembly + cache ─────────────────────────────────────────


@dataclass
class ContextBundle:
    agent: str
    generated_at: float
    expires_at: float
    sources: dict = field(default_factory=dict)
    size_bytes: int = 0
    cache_hit: bool = False

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "generated_at": self.generated_at,
            "expires_at": self.expires_at,
            "sources": self.sources,
            "size_bytes": self.size_bytes,
            "cache_hit": self.cache_hit,
        }

    def render(self) -> str:
        """Render bundle as a markdown block suitable for prompt injection."""
        parts: list[str] = []
        status = self.sources.get("status") or {}
        if status.get("content"):
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(status.get("updated_at") or 0))
            parts.append(f"### 📋 STATUS（{self.agent} 自己维护，更新于 {ts}）\n\n{status['content'].rstrip()}")
        main = self.sources.get("main_session") or {}
        turns = main.get("turns") or []
        if turns:
            lines = [f"### 🧵 主 session 最近 {len(turns)} 条 turn（{main.get('session_key', '')}）"]
            for t in turns:
                role = "🧑 you" if t.get("role") == "assistant" else "👤 user"
                txt = (t.get("text") or "").strip()
                if len(txt) > 600:
                    txt = txt[:600] + "…"
                lines.append(f"\n**{role}**: {txt}")
            if main.get("truncated"):
                lines.append("\n_(earlier turns dropped to fit budget)_")
            parts.append("\n".join(lines))
        memory = self.sources.get("memory") or {}
        hits = memory.get("hits") or []
        if hits:
            lines = [f"### 🧠 相关记忆（query: {memory.get('query', '')[:80]}）"]
            for h in hits:
                p = h.get("path", "?")
                snip = (h.get("snippet") or "").strip().replace("\n", " ")
                if len(snip) > 240:
                    snip = snip[:240] + "…"
                lines.append(f"- `{p}` — {snip}")
            parts.append("\n".join(lines))
        if not parts:
            return ""
        header = f"_OpenForge context bundle generated {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.generated_at))}_"
        return header + "\n\n" + "\n\n".join(parts)


def _cache_path(agent_id: str) -> Path:
    return _bundles_dir() / f"{agent_id}.json"


def _load_cached_bundle(agent_id: str) -> ContextBundle | None:
    p = _cache_path(agent_id)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    if float(d.get("expires_at") or 0) <= time.time():
        return None
    b = ContextBundle(
        agent=d.get("agent") or agent_id,
        generated_at=float(d.get("generated_at") or 0),
        expires_at=float(d.get("expires_at") or 0),
        sources=d.get("sources") or {},
        size_bytes=int(d.get("size_bytes") or 0),
        cache_hit=True,
    )
    return b


def _save_cached_bundle(b: ContextBundle) -> None:
    try:
        _cache_path(b.agent).write_text(
            json.dumps(b.to_dict(), ensure_ascii=False), encoding="utf-8",
        )
    except OSError:
        pass


def _invalidate_cache(agent_id: str) -> None:
    p = _cache_path(agent_id)
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass


def build_context_bundle(
    agent_id: str,
    query_hint: str | None = None,
    force_refresh: bool = False,
) -> ContextBundle:
    """Assemble the three-source bundle for `agent_id`.

    Returns a ContextBundle (possibly empty) — never raises for missing sources.
    """
    _validate_agent_id(agent_id)
    cfg = load_agent_config(agent_id)
    if not cfg.get("enabled", True):
        # Even when disabled, return an empty bundle so callers can no-op safely.
        now = time.time()
        return ContextBundle(
            agent=agent_id, generated_at=now, expires_at=now,
            sources={}, size_bytes=0,
        )
    if not force_refresh:
        cached = _load_cached_bundle(agent_id)
        if cached:
            return cached
    include = set(cfg.get("include") or [])
    sources: dict = {}
    if "status" in include:
        try:
            s = collect_status(agent_id, cfg)
            if s:
                sources["status"] = s
        except Exception:
            pass
    if "main_session" in include:
        try:
            m = collect_main_session(agent_id, cfg)
            if m:
                sources["main_session"] = m
        except Exception:
            pass
    if "memory" in include:
        try:
            mem = collect_memory(agent_id, cfg, query_hint)
            if mem:
                sources["memory"] = mem
        except Exception:
            pass
    now = time.time()
    ttl = int(cfg.get("cache_ttl_seconds") or 0)
    b = ContextBundle(
        agent=agent_id,
        generated_at=now,
        expires_at=now + max(ttl, 0),
        sources=sources,
        size_bytes=0,
        cache_hit=False,
    )
    try:
        rendered = b.render()
        b.size_bytes = len(rendered.encode("utf-8"))
    except Exception:
        b.size_bytes = 0
    if ttl > 0:
        _save_cached_bundle(b)
    return b


def reset_cache(agent_id: str | None = None) -> None:
    """Test helper: drop cached bundles (one agent, or all)."""
    d = _bundles_dir()
    if agent_id:
        _invalidate_cache(agent_id)
        return
    for p in d.glob("*.json"):
        try:
            p.unlink()
        except OSError:
            pass
