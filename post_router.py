"""
post_router.py — P0 post routing for OpenForge.

When scott posts a message that @mentions one or more agents, this module
spawns `openclaw agent` subprocesses (one per mention, sequentially) and
appends each agent reply as a new `post_added` event on the same thread.

Design constraints (see TODO.md):
- Reuses the snapshot/restore main-session logic from run_standup.py so the
  agent's primary chat session isn't hijacked by our `--session-id` calls.
- One worker thread per process is enough: agents share a main pointer, so
  routing must be serialized globally (a parallel call would clobber the
  snapshot of the other in-flight call).
- HTTP handler stays non-blocking: it enqueues and returns immediately.
- Failures are recorded as a synthetic `agent_router_failed` post by
  `__router__`, so the thread UI shows what went wrong without crashing.

Session-id convention: `forge-<thread_id>-<agent_id>` (stable per thread/agent
pair, so the agent keeps continuity across multiple turns in the same thread).
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

import forge_store as store
from run_standup import (
    AgentError,
    call_agent,
    clean,
    is_empty,
    restore_main,
    snapshot_main,
)

# ─── config ───────────────────────────────────────────────────────────
ROUTER_SPEAKER_FALLBACK = "__router__"
# How many prior posts of context to feed the agent. Most threads are short;
# 50 is plenty and bounds the prompt size.
MAX_CONTEXT_POSTS = 50


# ─── work queue ──────────────────────────────────────────────────────
# Single worker thread → strict serialization across all threads. The
# agent main-session pointer is shared global state; parallel routing of
# two posts (even on different threads) would race on snapshot/restore.
_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_loop, name="openforge-router",
                             daemon=True)
        t.start()
        _worker_started = True


def _worker_loop() -> None:
    while True:
        try:
            thread_id, trigger_post_id = _q.get()
        except Exception:
            time.sleep(0.5)
            continue
        try:
            _route_one(thread_id, trigger_post_id)
        except Exception as e:  # last-ditch; never let the worker die
            try:
                store.add_thread_post(
                    thread_id, ROUTER_SPEAKER_FALLBACK,
                    f"⚠️ post router crashed: {e!r}",
                )
                store.write_thread_markdown(thread_id)
            except Exception:
                pass


# ─── public api ──────────────────────────────────────────────────────
def enqueue_if_needed(thread_id: str, post: dict[str, Any]) -> bool:
    """Inspect a freshly-added post and queue routing work if applicable.

    Returns True if work was enqueued. Safe to call from the HTTP handler.
    """
    if not post:
        return False
    if (post.get("speaker") or "").strip().lower() != "scott":
        return False
    mentions = post.get("mentions") or []
    if not mentions:
        return False
    _ensure_worker()
    _q.put((thread_id, post.get("post_id") or ""))
    return True


# ─── core routing ────────────────────────────────────────────────────
def _route_one(thread_id: str, trigger_post_id: str) -> None:
    thread = store.project_thread(thread_id)
    if thread is None:
        return
    if thread.get("closed_at"):
        return  # closed mid-flight; skip silently

    # find the trigger post (fall back to last scott post with mentions)
    trigger = _find_trigger_post(thread, trigger_post_id)
    if not trigger:
        return
    mentions: list[str] = list(trigger.get("mentions") or [])
    if not mentions:
        return

    # dedupe while preserving order; route to each agent at most once per
    # trigger post.
    seen: set[str] = set()
    ordered = []
    for m in mentions:
        key = m.strip().lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)

    for agent_id in ordered:
        try:
            _route_to_agent(thread_id, agent_id, trigger)
        except Exception as e:
            try:
                store.add_thread_post(
                    thread_id, ROUTER_SPEAKER_FALLBACK,
                    f"⚠️ routing to @{agent_id} failed: {e!r}",
                )
            except Exception:
                pass
        finally:
            try:
                store.write_thread_markdown(thread_id)
            except Exception:
                pass


def _route_to_agent(thread_id: str, agent_id: str, trigger: dict) -> None:
    """Snapshot agent main → run one agent turn → restore → append reply."""
    session_id = f"forge-{thread_id}-{agent_id}"
    snap = snapshot_main(agent_id)
    try:
        prompt = _build_prompt(thread_id, agent_id, trigger)
        try:
            reply = call_agent(agent_id, session_id, prompt)
        except AgentError as e:
            store.add_thread_post(
                thread_id, ROUTER_SPEAKER_FALLBACK,
                f"⚠️ @{agent_id} 没回复: {e}",
            )
            return
        reply = clean(reply)
        if is_empty(reply):
            store.add_thread_post(
                thread_id, ROUTER_SPEAKER_FALLBACK,
                f"_(@{agent_id} 返回空回复)_",
            )
            return
        store.add_thread_post(thread_id, agent_id, reply)
    finally:
        if snap:
            try:
                restore_main(agent_id, snap)
            except Exception:
                pass


# ─── helpers ─────────────────────────────────────────────────────────
def _find_trigger_post(thread: dict, post_id: str) -> dict | None:
    posts = thread.get("posts") or []
    if post_id:
        for p in posts:
            if p.get("post_id") == post_id and not p.get("superseded"):
                return p
    # fallback: most recent non-superseded scott post with mentions
    for p in reversed(posts):
        if p.get("superseded"):
            continue
        if (p.get("speaker") or "").lower() != "scott":
            continue
        if p.get("mentions"):
            return p
    return None


def _build_prompt(thread_id: str, agent_id: str, trigger: dict) -> str:
    """Render the thread as a transcript and ask the agent for one reply."""
    md = store.render_thread_markdown(thread_id) or "(空 thread)"
    md_for_prompt = "\n".join(
        l for l in md.splitlines() if not l.startswith("<!--")
    )
    trigger_preview = (trigger.get("content") or "").strip()
    return (
        f"你正在参加 OpenForge 一个 thread 的讨论，scott 在最新一条 post "
        f"里 @ 了你（{agent_id}）。下面是 thread 当前的完整内容：\n\n"
        f"━━━ Thread ━━━\n{md_for_prompt}\n━━━ 结束 ━━━\n\n"
        f"[你的身份]: {agent_id}\n"
        f"[scott 刚刚 @ 你的那条 post]:\n{trigger_preview}\n\n"
        f"要求：\n"
        f"- 用中文，简洁段落（不要 markdown 标题）\n"
        f"- 你的回复会被独立保存为这条 thread 的一条新 post\n"
        f"- 直接针对 scott 的最新 post 回应，不要复述之前的内容\n"
        f"- 如果 scott 的问题已经被回答过或不需要你回答，回复 `completed`\n"
    )
