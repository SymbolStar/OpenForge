"""
forge_xiaof_openai — OpenAI-compatible adapter for the 小F backend.

Implements the agreed main path (Scott 2026-06-29 拍板):

    POST /api/xiaof/ask  →  general_qa  →  OpenAI-compatible chat.completions
                                            (DeepSeek / Qwen / OpenAI / vLLM /
                                             eventually the milk-owned gateway)

A-10 "换 backend 改 1 个配置" is delivered as three env vars:

    XIAOF_OPENAI_BASE_URL   e.g. https://api.deepseek.com/v1  (or gateway base)
    XIAOF_OPENAI_API_KEY    bearer token; if unset the adapter is considered
                            disabled and the route falls back to stub.
    XIAOF_OPENAI_MODEL      defaults to "deepseek-chat".
    XIAOF_OPENAI_TIMEOUT    seconds, default 30.

Routing (in forge_xiaof.default_adapter):

  - intent=general_qa  AND  local A-4 builtin doesn't match
                       AND  XIAOF_OPENAI_* env present
      → this adapter (real LLM)
  - everything else    → stub_adapter (M2 owns thread_search later)

Red lines kept identical to PRD v0.2 / API contract v0.1:
  - Event order:   meta → token* → chips → done   (chips always empty here;
                   thread_search never reaches this adapter, A-7 12-case
                   fixture is unaffected — LLM cannot pull threads).
  - Error codes:   upstream/network failure → XiaofRequestError(code=
                   "upstream_failed"); the route maps it to the contract's
                   4-error set, never `forbidden`.
  - User-visible jargon scrubber:  every emitted token chunk passes through
                                   scrub_jargon() so the LLM cannot
                                   accidentally leak "M1 / M2 / stub /
                                   adapter / milestone / 占位" into user
                                   text.  A streaming look-ahead buffer
                                   prevents partial-word splits across
                                   token boundaries.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from collections.abc import Iterator, Mapping
from typing import Any

# ── jargon scrubber ────────────────────────────────────────────────

# Same forbidden set the stub regression test pins, applied to *LLM*
# output too. Case-insensitive for ASCII; CJK kept verbatim.
_FORBIDDEN_WORDS: tuple[str, ...] = (
    "M1",
    "M2",
    "stub",
    "adapter",
    "milestone",
    "占位",
)

_FORBIDDEN_RE = re.compile(
    r"(?i)(?:" + "|".join(re.escape(w) for w in _FORBIDDEN_WORDS) + r")"
)

# Streaming look-ahead: keep this many tail chars buffered before emitting
# so we never split a forbidden word across two `token` events.
_LOOKAHEAD = max(len(w) for w in _FORBIDDEN_WORDS) + 1


def scrub_jargon(text: str) -> str:
    """Mask any forbidden engineering jargon. Preserves character count
    so streaming offsets remain consistent."""

    def _mask(m: re.Match[str]) -> str:
        return "·" * len(m.group(0))

    return _FORBIDDEN_RE.sub(_mask, text)


def _split_emit_buffer(buf: str) -> tuple[str, str]:
    """Return (safe_to_emit_scrubbed, keep_buffered).

    Invariant: no forbidden match may be bisected by the cut, otherwise
    the two halves would be emitted in separate `token` events and
    reassemble into the original word in the user's DOM.
    """
    if len(buf) <= _LOOKAHEAD:
        return "", buf
    cut = len(buf) - _LOOKAHEAD
    for m in _FORBIDDEN_RE.finditer(buf):
        # If the match straddles the cut point, pull the cut back to
        # the match start so the whole word stays buffered (or, if the
        # match is wholly in the safe prefix, leave cut alone — it gets
        # scrubbed before emission).
        if m.start() < cut < m.end():
            cut = m.start()
        elif m.start() >= cut:
            # Subsequent matches are all in the retained tail.
            break
    return scrub_jargon(buf[:cut]), buf[cut:]


# ── env config / gating ────────────────────────────────────────────


def openai_enabled() -> bool:
    """True when both base_url and api_key are configured. Gateway path
    (milk-owned) is just a different base_url — adapter doesn't care."""
    return bool(
        os.environ.get("XIAOF_OPENAI_API_KEY")
        and os.environ.get("XIAOF_OPENAI_BASE_URL")
    )


def _config() -> dict[str, Any]:
    return {
        "base_url": os.environ["XIAOF_OPENAI_BASE_URL"].rstrip("/"),
        "api_key": os.environ["XIAOF_OPENAI_API_KEY"],
        "model": os.environ.get("XIAOF_OPENAI_MODEL", "deepseek-chat"),
        "timeout": float(os.environ.get("XIAOF_OPENAI_TIMEOUT", "30")),
    }


# ── system prompt ──────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "你是 OpenForge 的小F，一个简洁、直接的问答助手。"
    "回答使用中文，控制在 200 字以内，自然口吻。"
    "不要谈论自己是 AI / 模型 / 后端实现，不要使用工程黑话。"
    "你看不到也不能讨论任何跨 thread 检索结果 —— 检索由独立路径处理；"
    "如果用户在问历史 thread，礼貌告诉对方检索功能正在接通中，"
    "并先回答 ta 的通用问题部分。"
)


# ── HTTP layer (override-friendly for tests) ───────────────────────


def _urlopen(req: urllib.request.Request, timeout: float):  # pragma: no cover - thin
    return urllib.request.urlopen(req, timeout=timeout)


# ── adapter ────────────────────────────────────────────────────────


def openai_compatible_adapter(
    payload: Mapping[str, Any],
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """OpenAI-compatible streaming adapter.

    Emits the standard meta → token* → chips → done sequence. Chips is
    always empty here — this adapter never participates in retrieval, so
    A-7's 12-case ACL fixture (M2) is structurally unable to be bypassed.
    """
    # Late import avoids forge_xiaof ↔ forge_xiaof_openai circular dep.
    from forge_xiaof import XiaofRequestError, classify_intent, new_request_id

    started = time.monotonic()
    query = str(payload.get("query") or "")
    intent = classify_intent(query)
    request_id = new_request_id()

    yield (
        "meta",
        {"intent": intent, "request_id": request_id, "provider": "openai-compat"},
    )

    try:
        config = _config()
    except KeyError as exc:
        raise XiaofRequestError(
            f"openai adapter missing config: {exc!s}", code="upstream_failed"
        ) from exc

    request_body = {
        "model": config["model"],
        "stream": True,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
    }
    req = urllib.request.Request(
        config["base_url"] + "/chat/completions",
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + config["api_key"],
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    try:
        resp = _urlopen(req, timeout=config["timeout"])
    except Exception as exc:  # noqa: BLE001
        raise XiaofRequestError(
            f"openai upstream failed: {exc!r}", code="upstream_failed"
        ) from exc

    buffer = ""
    emitted_chars = 0

    try:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data or data == "[DONE]":
                if data == "[DONE]":
                    break
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta_obj = choices[0].get("delta") or {}
            delta = delta_obj.get("content")
            if not isinstance(delta, str) or not delta:
                continue
            buffer += delta
            emit, buffer = _split_emit_buffer(buffer)
            if emit:
                yield ("token", {"text": emit})
                emitted_chars += len(emit)
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass

    # Final flush — anything left in the look-ahead buffer must still be
    # scrubbed before reaching the user.
    if buffer:
        tail = scrub_jargon(buffer)
        if tail:
            yield ("token", {"text": tail})
            emitted_chars += len(tail)

    yield ("chips", {"chips": [], "chip_total": 0})
    yield (
        "done",
        {
            "latency_ms": int((time.monotonic() - started) * 1000),
            "chip_count": 0,
            "chip_total": 0,
            "tokens_in": len(query),
            "tokens_out": emitted_chars,
            "provider": "openai-compat",
            "model": config["model"],
        },
    )
