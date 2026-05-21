"""
post_router.py — post routing for OpenForge.

When scott posts a message that @mentions one or more agents, this module
spawns `openclaw agent` subprocesses (one per mention) and appends each
agent reply as a new `post_added` event on the same thread.

Design:
- `openclaw agent --local --json` is fully sandboxed in a subprocess: it does
  NOT mutate `agent:<id>:main` on the host, so we can fan out N concurrent
  invocations without racing on a shared snapshot. We keep snapshot/restore
  available as a defensive belt-and-suspenders, but it's a no-op on --local
  paths.
- One daemon thread per (thread_id, agent_id) mention, bounded by a global
  semaphore (`MAX_PARALLEL_ROUTES`, env `OPENFORGE_MAX_PARALLEL_ROUTES`).
- Same (thread, agent) pair is deduped while in flight so scott double-tapping
  `@milk` does not spawn two milk processes sharing the same forge-<tid>-<agent>
  session-id (they would race on the jsonl file).
- HTTP handler stays non-blocking: enqueue dispatches workers and returns.
- Failures are recorded as a synthetic post by `__router__` so the thread UI
  shows what went wrong instead of silently hanging.

Session-id convention: `forge-<thread_id>-<agent_id>` (stable per thread/agent
pair, so the agent keeps continuity across multiple turns in the same thread).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

import forge_store as store
from agent_runtime import (
    AGENTS_ROOT,
    AgentError,
    _find_clean_main,
    _is_forge_sid,
    _sessions_path,
    call_agent,
    clean,
    is_empty,
    restore_main,
    snapshot_main,
)
import time

# ─── config ──────────────────────────────────────────────────────────
ROUTER_SPEAKER_FALLBACK = "__router__"

# Concurrency cap across all in-flight agent subprocesses. --local agent
# runs are fully sandboxed in their own subprocess + ephemeral session, so
# we no longer need single-flight serialization to protect agent main
# pointers. The cap is just so a flood of @mentions doesn't fork-bomb.
MAX_PARALLEL_ROUTES = int(os.environ.get("OPENFORGE_MAX_PARALLEL_ROUTES", "6"))

# Soft dedupe set of (thread_id, agent_id) currently being routed. Same
# pair is dropped silently if re-enqueued while still running.
_inflight: set[tuple[str, str]] = set()
_inflight_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_PARALLEL_ROUTES)

# How many prior posts to feed the agent. Bounds the prompt size.
MAX_CONTEXT_POSTS = 50


# ─── public api ──────────────────────────────────────────────────────
def enqueue_if_needed(thread_id: str, post: dict[str, Any]) -> bool:
    """Inspect a freshly-added post and dispatch routing workers if applicable.

    Returns True if at least one agent route was dispatched. Safe to call
    from the HTTP handler: each (thread, agent) pair runs on its own daemon
    thread, bounded by MAX_PARALLEL_ROUTES.
    """
    if not post:
        return False
    if (post.get("speaker") or "").strip().lower() != "scott":
        return False
    mentions = list(post.get("mentions") or [])
    # Implicit mention: if scott replies (parent_post_id set) to an agent
    # post without an explicit @, treat it as @<that agent>. Mirrors Slack
    # / Discord thread-reply semantics. Replies to scott's own or to
    # __router__ placeholder/error posts are ignored.
    if not mentions:
        implicit = _implicit_mention_from_parent(thread_id, post.get("parent_post_id"))
        if implicit:
            mentions = [implicit]
    if not mentions:
        return False

    # dedupe mentions while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for m in mentions:
        key = (m or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    if not ordered:
        return False

    trigger_pid = post.get("post_id") or post.get("id") or ""
    dispatched = False
    for agent_id in ordered:
        if _dispatch(thread_id, agent_id, trigger_pid):
            dispatched = True
    return dispatched


_RESERVED_SPEAKERS = {"scott", ROUTER_SPEAKER_FALLBACK.lower()}


def _implicit_mention_from_parent(thread_id: str, parent_post_id: str | None) -> str | None:
    """If parent_post_id points at an agent post, return that agent_id."""
    if not parent_post_id:
        return None
    thread = store.project_thread(thread_id)
    if not thread:
        return None
    parent = (thread.get("posts_by_id") or {}).get(parent_post_id)
    if not parent:
        return None
    speaker = (parent.get("speaker") or "").strip()
    if not speaker or speaker.lower() in _RESERVED_SPEAKERS:
        return None
    return speaker



def _dispatch(thread_id: str, agent_id: str, trigger_pid: str) -> bool:
    """Spawn a worker thread for one (thread, agent) pair unless already running."""
    key = (thread_id, agent_id)
    with _inflight_lock:
        if key in _inflight:
            return False  # drop the duplicate silently
        _inflight.add(key)
    t = threading.Thread(
        target=_run_one,
        args=(thread_id, agent_id, trigger_pid),
        name=f"openforge-route-{agent_id}-{thread_id[-6:]}",
        daemon=True,
    )
    t.start()
    return True


def _run_one(thread_id: str, agent_id: str, trigger_pid: str) -> None:
    """Worker entry: bounded-parallel agent invocation for a single mention."""
    key = (thread_id, agent_id)
    # Bound total in-flight subprocesses. (thread, agent) dedupe is enforced
    # BEFORE we wait on the semaphore so duplicates don't pile up in the
    # slot queue.
    try:
        with _slots:
            try:
                _route_to_agent_safely(thread_id, agent_id, trigger_pid)
            except Exception as e:
                _record_crash(thread_id, agent_id, e)
    finally:
        with _inflight_lock:
            _inflight.discard(key)


def _record_crash(thread_id: str, agent_id: str, e: BaseException) -> None:
    try:
        store.add_thread_post(
            thread_id, ROUTER_SPEAKER_FALLBACK,
            f"⚠️ post router crashed routing @{agent_id}: {e!r}",
        )
        store.write_thread_markdown(thread_id)
    except Exception:
        pass


# ─── core routing ────────────────────────────────────────────────────
def _route_to_agent_safely(thread_id: str, agent_id: str, trigger_pid: str) -> None:
    thread = store.project_thread(thread_id)
    if thread is None:
        return
    if thread.get("closed_at"):
        return  # closed mid-flight; skip silently
    trigger = _find_trigger_post(thread, trigger_pid)
    if not trigger:
        return
    # We intentionally do NOT re-check `agent_id in trigger.mentions` here:
    # the dispatcher (enqueue_if_needed) is the single source of truth for
    # who to route to (it also resolves implicit-mention-via-reply), and
    # re-deriving here would drop those.
    try:
        _route_to_agent(thread_id, agent_id, trigger)
    finally:
        try:
            store.write_thread_markdown(thread_id)
        except Exception:
            pass


def _route_to_agent(thread_id: str, agent_id: str, trigger: dict) -> None:
    """Run one agent turn → append reply. Concurrency-safe."""
    session_id = f"forge-{thread_id}-{agent_id}"
    trigger_pid = trigger.get("post_id") or trigger.get("id")
    # Step 1: announce we are working so the UI shows progress immediately.
    placeholder_id: str | None = None
    try:
        ph = store.add_thread_post(
            thread_id, ROUTER_SPEAKER_FALLBACK,
            f"⏳ @{agent_id} 正在思考中…",
            parent_post_id=trigger_pid,
        )
        placeholder_id = ph.get("post_id")
        store.write_thread_markdown(thread_id)
    except Exception:
        pass

    snap = snapshot_main(agent_id)
    final_post_id: str | None = None
    try:
        prompt = _build_prompt(thread_id, agent_id, trigger)
        try:
            reply = call_agent(agent_id, session_id, prompt)
        except AgentError as e:
            err = store.add_thread_post(
                thread_id, ROUTER_SPEAKER_FALLBACK,
                f"⚠️ @{agent_id} 没回复: {e}",
                parent_post_id=trigger_pid,
            )
            final_post_id = err.get("post_id")
            return
        reply = clean(reply)
        if is_empty(reply):
            err = store.add_thread_post(
                thread_id, ROUTER_SPEAKER_FALLBACK,
                f"_(@{agent_id} 返回空回复)_",
                parent_post_id=trigger_pid,
            )
            final_post_id = err.get("post_id")
            return
        added = store.add_thread_post(
            thread_id, agent_id, reply, parent_post_id=trigger_pid,
        )
        final_post_id = added.get("post_id")
    finally:
        if snap:
            try:
                restore_main(agent_id, snap)
            except Exception:
                pass
        # supersede the placeholder so it doesn't clutter the timeline.
        if placeholder_id:
            try:
                store.supersede_thread_post(
                    thread_id, placeholder_id, by_post_id=final_post_id,
                )
            except Exception:
                pass


# ─── helpers ─────────────────────────────────────────────────────────
def _find_trigger_post(thread: dict, post_id: str) -> dict | None:
    posts = thread.get("posts") or []
    if post_id:
        for p in posts:
            pid = p.get("post_id") or p.get("id")
            if pid == post_id and not p.get("superseded"):
                p.setdefault("post_id", pid)
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


# ─── startup self-heal ──────────────────────────────────────────────
def heal_polluted_mains(agent_ids: list[str]) -> list[str]:
    """On server boot, fix any agent main pointer left stuck on a forge-* sid.

    Returns the list of agent_ids we actually healed.
    """
    healed: list[str] = []
    for ag in agent_ids:
        p = _sessions_path(ag)
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        key = f"agent:{ag}:main"
        main = d.get(key)
        if not isinstance(main, dict):
            continue
        if not _is_forge_sid(main.get("sessionId") or ""):
            continue
        recovered = _find_clean_main(ag)
        if not recovered:
            continue
        main["sessionId"] = recovered["sessionId"]
        main["sessionFile"] = recovered["sessionFile"]
        main["updatedAt"] = int(time.time() * 1000)
        main["healedByOpenForge"] = True
        d[key] = main
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2))
        os.replace(tmp, p)
        healed.append(ag)
    return healed
