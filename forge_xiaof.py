"""
forge_xiaof — 小F (global agent) backend, M1 milestone.

M1 scope (per [[alice/global-agent-API-contract-v0.1.md]] §7):

  POST /api/xiaof/ask  →  SSE stream with FIXED event order
                          meta → token* → chips → done

  - Adapter abstraction so backend can be swapped (codex / self-hosted)
    without touching the route.  M1 ships a deterministic StubAdapter so
    the front-end (bobby PR #33 `window.xiaofAsk`) can wire to a real
    endpoint immediately and we can exercise the SSE contract end-to-end.

  - M2 will plug a real retrieval adapter behind the same interface
    (lexical/FTS + viewer ACL).  M3 adds embedding + monitoring + the
    12-case A-7 ACL fixture (owned by @milk as A-7 gate).

  - Error codes are restricted to the four declared in §2:
        unauth | rate_limited | upstream_failed | internal
    `forbidden` MUST NOT appear — A-7.5 red line.  Unauthorized retrieval
    must fall through to empty chips + a generic "no match" body so the
    client cannot distinguish from "really no match".

The adapter yields a stream of tagged events:

    ("meta",  {...})           # exactly one, first
    ("token", {"text": "..."}) # zero or more
    ("chips", {"chips": [...], "chip_total": N})  # exactly one
    ("done",  {...})           # exactly one, last

The route is responsible for serialising those tuples to SSE frames
according to the contract.  Adapters never write to the socket directly.
"""

from __future__ import annotations

import json as _json
import re
import secrets
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

try:  # py3.9+
    from zoneinfo import ZoneInfo  # type: ignore
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# ── public types ────────────────────────────────────────────────────

XiaofEvent = tuple[str, Mapping[str, Any]]
Adapter = Callable[[Mapping[str, Any]], Iterator[XiaofEvent]]


# ── helpers ─────────────────────────────────────────────────────────

def new_request_id() -> str:
    """Stable-ish request id for tracing.  Format: `xfr_<8 hex>`."""
    return "xfr_" + secrets.token_hex(4)


def classify_intent(query: str) -> str:
    """
    Minimal stub intent classifier.  M1 only distinguishes:

      - "thread_search" — anything containing the markers we use as a
        cheap proxy for "user is looking for past discussion".
      - "general_qa"    — everything else.

    M2 will replace this with the real retrieval intent gate.  We keep
    this *here* (not in the route) so adapters can override it later
    without touching the SSE plumbing.
    """
    if not query:
        return "general_qa"
    q = query.lower()
    triggers = (
        "thread",
        "上次",
        "之前",
        "讨论",
        "决定",
        "哪条",
        "post",
        "在哪",
        "找",
        "search",
    )
    return "thread_search" if any(t in q for t in triggers) else "general_qa"


# ── light general-QA built-ins ─────────────────────────────────────
#
# Until M2 plugs in codex, the stub still has to satisfy PRD A-4
# ("asking the time must be answered in ≤5s"). We answer a small
# closed set of trivially deterministic questions locally so the user
# never sees a placeholder for things the agent obviously can do.
# Anything we can't answer falls back to a *neutral* user-facing copy
# — no "M1 / M2 / stub" leakage allowed in user-visible text (alice's
# product red line, 2026-05-28).

_TIME_QUERY_RE = re.compile(
    r"(几点|什么时间|现在时间|当前时间|当前时刻|现在.*时刻|what.?s the time|current time|time now|date today|今天.*几号|今天.*日期|today.?s date)",
    re.IGNORECASE,
)

# A tiny city → IANA tz map. Kept intentionally small; M2 wires the
# real timezone resolver. Lowercased keys.
_TZ_HINTS: dict[str, str] = {
    "asia/shanghai": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "上海": "Asia/Shanghai",
    "北京": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "asia/tokyo": "Asia/Tokyo",
    "tokyo": "Asia/Tokyo",
    "东京": "Asia/Tokyo",
    "america/new_york": "America/New_York",
    "new york": "America/New_York",
    "纽约": "America/New_York",
    "europe/london": "Europe/London",
    "london": "Europe/London",
    "伦敦": "Europe/London",
    "utc": "UTC",
    "gmt": "UTC",
}


def _resolve_tz(query: str, client: Mapping[str, Any]) -> str | None:
    """Pick a tz: explicit query mention > client.tz > None."""
    q = query.lower()
    for key, tz in _TZ_HINTS.items():
        if key in q:
            return tz
    tz = client.get("tz") if isinstance(client, Mapping) else None
    if isinstance(tz, str) and tz:
        return tz
    return None


def answer_general_qa(query: str, client: Mapping[str, Any]) -> str | None:
    """
    Local quick-answer for a tiny closed set of general questions.

    Returns the answer string, or None if we have no built-in handler
    (caller then falls back to the neutral user-facing copy).
    """
    if not query:
        return None
    if _TIME_QUERY_RE.search(query):
        tz_name = _resolve_tz(query, client) or "UTC"
        try:
            if ZoneInfo is None:
                raise RuntimeError("zoneinfo unavailable")
            now = datetime.now(ZoneInfo(tz_name))
        except Exception:
            # Unknown tz string — degrade to UTC rather than apologise.
            tz_name = "UTC"
            now = datetime.now(UTC)
        return (
            f"{tz_name} 当前时间是 "
            f"{now.strftime('%Y-%m-%d %H:%M')} "
            f"({now.strftime('%A')})."
        )
    return None


# User-facing fallback copy. Intentionally neutral, no milestone /
# implementation jargon. Two variants so general_qa and thread_search
# read naturally to the user; both still produce identical wire shape
# (empty chips) so the A-7.6 unauthorized-vs-no-match
# indistinguishability is preserved.
_FALLBACK_GENERAL = "暂时还回答不了这条问题。跨 thread 检索功能也正在接通中，稍后就能帮你找历史讨论。"
_FALLBACK_SEARCH = "跨 thread 检索功能正在接通中，稍后就能帮你找到对应的历史讨论。临时问题可以直接问我。"


# ── stub adapter ────────────────────────────────────────────────────

def stub_adapter(
    payload: Mapping[str, Any],
    *,
    now: Callable[[], float] = time.monotonic,
) -> Iterator[XiaofEvent]:
    """
    Deterministic stub adapter.

    Emits the four-phase event stream so the front-end can validate its
    parser end-to-end before any retrieval is wired up.  No real codex
    call, no retrieval — body text is canned, chips list is empty.

    A-7.5 compliance: even though there is no ACL evaluated here, the
    *shape* matches the unauthorized-result case (empty chips, generic
    "no match" body), so the front-end's zero-state path is exercised
    by the same code path as the real "no match" case.  This is the
    reason chips is `[]` rather than a fake demo card.
    """
    started = now()
    request_id = new_request_id()
    query = str(payload.get("query") or "")
    client = payload.get("client") if isinstance(payload, Mapping) else None
    if not isinstance(client, Mapping):
        client = {}
    intent = classify_intent(query)

    yield ("meta", {"intent": intent, "request_id": request_id})

    # Pick body text:
    #   1. general_qa with a local built-in answer (time / date) → use it.
    #   2. otherwise neutral fallback copy, branched by intent so the
    #      sentence reads naturally. NEVER include milestone / stub
    #      jargon ("M1" / "M2" / "stub" / "adapter") in user-visible
    #      text — alice's product red line, 2026-05-28.
    body: str | None = None
    if intent == "general_qa":
        body = answer_general_qa(query, client)
    if body is None:
        body = _FALLBACK_SEARCH if intent == "thread_search" else _FALLBACK_GENERAL

    for chunk in _chunk_text(body, size=4):
        yield ("token", {"text": chunk})

    yield ("chips", {"chips": [], "chip_total": 0})

    latency_ms = int((now() - started) * 1000)
    yield (
        "done",
        {
            "latency_ms": latency_ms,
            "chip_count": 0,
            "chip_total": 0,
            "tokens_in": len(query),
            "tokens_out": len(body),
        },
    )


def _chunk_text(text: str, size: int) -> Iterable[str]:
    if size <= 0:
        yield text
        return
    for i in range(0, len(text), size):
        yield text[i : i + size]


# ── adapter registry ────────────────────────────────────────────────

_ADAPTERS: dict[str, Adapter] = {"stub": stub_adapter}
_DEFAULT_ADAPTER_NAME = "default"


def default_adapter(payload: Mapping[str, Any]) -> Iterator[XiaofEvent]:
    """
    Routing adapter (the one the route looks up by default).

    Decision matrix:
      - intent == general_qa
          └ local A-4 builtin matches (time/date/tz) → stub (no LLM call)
          └ OpenAI-compat env present                  → openai_compatible_adapter
          └ otherwise                                   → stub neutral copy
      - intent == thread_search                          → stub (M2 owns this)

    Keeping the local builtin first preserves PRD A-4 sub-5s latency for
    time queries even when an LLM is wired up; the LLM only ever sees
    open-ended general questions, never thread retrieval.
    """
    # Late import: forge_xiaof_openai depends on this module, so we
    # cannot import it at module load time.
    try:
        from forge_xiaof_openai import openai_compatible_adapter, openai_enabled
    except Exception:  # noqa: BLE001 - defensive
        openai_compatible_adapter = None  # type: ignore[assignment]
        openai_enabled = lambda: False  # noqa: E731

    query = str(payload.get("query") or "")
    intent = classify_intent(query)
    client = payload.get("client") if isinstance(payload, Mapping) else None
    if not isinstance(client, Mapping):
        client = {}

    if (
        intent == "general_qa"
        and answer_general_qa(query, client) is None
        and openai_enabled()
        and openai_compatible_adapter is not None
    ):
        yield from openai_compatible_adapter(payload)
        return

    yield from stub_adapter(payload)


_ADAPTERS["default"] = default_adapter


def register_adapter(name: str, adapter: Adapter) -> None:
    """Plug a new adapter at runtime (used by M2/M3 wiring + tests)."""
    if not isinstance(name, str) or not name:
        raise ValueError("adapter name required")
    _ADAPTERS[name] = adapter


def get_adapter(name: str | None = None) -> Adapter:
    """Look up an adapter by name; falls back to the default ('stub')."""
    if name is None:
        name = _DEFAULT_ADAPTER_NAME
    if name not in _ADAPTERS:
        raise KeyError(f"unknown xiaof adapter: {name!r}")
    return _ADAPTERS[name]


def set_default_adapter(name: str) -> None:
    """Switch the default adapter (M2 will flip this from config)."""
    global _DEFAULT_ADAPTER_NAME
    if name not in _ADAPTERS:
        raise KeyError(f"unknown xiaof adapter: {name!r}")
    _DEFAULT_ADAPTER_NAME = name


# ── request validation ─────────────────────────────────────────────

class XiaofRequestError(Exception):
    """Raised when /api/xiaof/ask request body is malformed."""

    def __init__(self, message: str, *, code: str = "internal"):
        super().__init__(message)
        self.code = code


def validate_ask_request(opts: Any) -> dict[str, Any]:
    """
    Validate + normalise the JSON body for POST /api/xiaof/ask.

    Returns a normalised dict.  Raises XiaofRequestError on any problem;
    the route maps the error to the appropriate SSE `error` event with
    the contract-approved code (never `forbidden`).
    """
    if not isinstance(opts, dict):
        raise XiaofRequestError("body must be a JSON object")
    query = opts.get("query")
    if not isinstance(query, str) or not query.strip():
        raise XiaofRequestError("query is required")
    if len(query) > 4000:
        raise XiaofRequestError("query too long: max 4000 chars")
    session_id = opts.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise XiaofRequestError("session_id must be string when provided")
    # viewer is normally injected by the gateway; in MVP loopback it can
    # be passed directly.  We do NOT trust it for ACL — the ACL adapter
    # (M2) is responsible for re-resolving viewer identity server-side.
    viewer = opts.get("viewer")
    if viewer is not None and not isinstance(viewer, str):
        raise XiaofRequestError("viewer must be string when provided")
    client = opts.get("client")
    if client is not None and not isinstance(client, dict):
        raise XiaofRequestError("client must be object when provided")
    return {
        "query": query.strip(),
        "session_id": session_id or "",
        "viewer": viewer or "",
        "client": client or {},
    }


# ── SSE frame builders ─────────────────────────────────────────────


def sse_frame(event: str, data: Mapping[str, Any]) -> str:
    """
    Build a single SSE frame (`event:` + `data:` + blank line).

    Kept here so adapter tests and route tests share one formatter and
    we can't drift on framing details (e.g. trailing newline counts,
    JSON unicode handling).
    """
    payload = _json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def stream_to_sse_frames(events: Iterable[XiaofEvent]) -> Iterator[str]:
    """
    Wrap an adapter's event stream into SSE frames, enforcing the
    contract's event order: meta → token* → chips → done.

    Raises ValueError on any out-of-order or duplicate phase event —
    this trips loudly in tests so an adapter author can't silently
    break the front-end's parser assumptions.
    """
    seen_meta = False
    seen_chips = False
    seen_done = False
    for event_name, data in events:
        if event_name == "meta":
            if seen_meta:
                raise ValueError("duplicate meta event")
            if seen_chips or seen_done:
                raise ValueError("meta must come before chips/done")
            seen_meta = True
        elif event_name == "token":
            if not seen_meta:
                raise ValueError("token before meta")
            if seen_chips or seen_done:
                raise ValueError("token after chips/done")
        elif event_name == "chips":
            if not seen_meta:
                raise ValueError("chips before meta")
            if seen_chips:
                raise ValueError("duplicate chips event")
            if seen_done:
                raise ValueError("chips after done")
            seen_chips = True
        elif event_name == "done":
            if not seen_chips:
                raise ValueError("done before chips")
            if seen_done:
                raise ValueError("duplicate done event")
            seen_done = True
        elif event_name == "error":
            # error terminates the stream; we don't enforce ordering on
            # it (the route may inject it mid-stream).
            pass
        else:
            raise ValueError(f"unknown event kind: {event_name!r}")
        yield sse_frame(event_name, data)

    if not seen_done and not seen_meta:
        # Empty stream — emit nothing rather than half-write headers.
        return
    if not seen_done:
        raise ValueError("stream ended before done event")
