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
import re
import threading
import time
from pathlib import Path
from typing import Any

import forge_employees
import forge_identity
import forge_store as store
from agent_runtime import (
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

try:
    import forge_context  # type: ignore
except Exception:  # pragma: no cover
    forge_context = None  # type: ignore

import forge_project  # PR-B1: shared project_dir validator (lazy + 60s cache)

# ─── config ──────────────────────────────────────────────────────────
ROUTER_SPEAKER_FALLBACK = "__router__"
STATUS_PHASES = {"thinking", "running", "done", "failed", "skipped"}
ERROR_TAIL_LIMIT = 2048

# Concurrency cap across all in-flight agent subprocesses. --local agent
# runs are fully sandboxed in their own subprocess + ephemeral session, so
# we no longer need single-flight serialization to protect agent main
# pointers. The cap is just so a flood of @mentions doesn't fork-bomb.
MAX_PARALLEL_ROUTES = int(os.environ.get("OPENFORGE_MAX_PARALLEL_ROUTES", "6"))

# Soft dedupe set of (thread_id, agent_id) currently being routed. Same
# pair is dropped silently if re-enqueued while still running.
_inflight: set[tuple[str, str]] = set()
_inflight_lock = threading.Lock()

# Set to True by drain_and_terminate() when forge is shutting down.
# Once set, enqueue_if_needed() refuses to spawn new workers — those
# triggers will be picked up as orphan placeholders on the next boot
# (see recover_orphan_placeholders). We never reset this within a
# process: a forge that's been told to drain has no business spawning
# new workers, even if a stray trigger sneaks in mid-shutdown.
_draining = False


def is_draining() -> bool:
    return _draining


def drain_and_terminate(grace_seconds: float = 8.0) -> int:
    """Tell forge to stop accepting new dispatches and SIGTERM every
    in-flight openclaw subprocess group.

    Used from the server's SIGTERM handler so OpenClaw subprocesses get
    a chance to run their own cleanup (releaseAllLocksSync → session
    lockfile cleared). Without this, forge dying via SIGTERM/SIGKILL
    leaves openclaw children orphaned to init, holding their session
    lockfiles indefinitely.

    Returns the number of openclaw subprocess groups we SIGTERMed.
    """
    global _draining
    _draining = True
    # Import here to avoid a top-level circular import.
    import agent_runtime
    return agent_runtime.terminate_all_active(grace_seconds=grace_seconds)

_slots = threading.BoundedSemaphore(MAX_PARALLEL_ROUTES)

# How many prior posts to feed the agent. Bounds the prompt size.
MAX_CONTEXT_POSTS = 50

# Handoff verbs / phrases that look like "X is the next person to act".
# Used by _detect_missing_handoff_mentions to flag prose like
# "alice 可以动了" / "交给 milk" / "等 dora review" — prose that names a
# teammate as the next actor but lacks the @<id> the router needs.
# Conservative on purpose: false positives are visible as a hint post,
# so we'd rather under-detect than spam threads.
_HANDOFF_VERB_RE = re.compile(
    r"可以动|可以接力|接力|交给|交接|接下来|轮到你|请你|需要你|等你|靠你|由你|你接手|你 review|你看|你拍|review 一下|一下"
)

# Max characters between teammate-name token and a handoff verb for the
# pair to count as a handoff. Tight to avoid "alice did X. Separately, foo
# needs review" false positives.
_HANDOFF_WINDOW = 25


def _detect_missing_handoff_mentions(
    reply: str, mentions: list[str], speaker: str
) -> list[str]:
    """Return employee ids the agent named as a handoff target without @-ing.

    A miss requires ALL of:
      - the reply contains a known employee's id or display name,
      - within ±{_HANDOFF_WINDOW} chars there's a handoff verb,
      - that employee is NOT in the parsed `mentions` list,
      - that employee is not the speaker themselves.

    Returns a stable, deduped list in detection order. Failures inside
    are swallowed (returns []) so the router never raises on a hint.
    """
    try:
        if not reply:
            return []
        # Normalise already-parsed mentions to canonical agent ids.
        mentioned_ids: set[str] = set()
        for m in mentions or []:
            resolved = forge_identity.name_to_id(m) or (m or "").strip().lower()
            if resolved:
                mentioned_ids.add(resolved)
        spk = (speaker or "").strip().lower()
        lower_reply = reply.lower()
        employee_ids = forge_employees.list_employees()

        misses: list[str] = []
        seen: set[str] = set()
        for aid in employee_ids:
            if aid == spk or aid in mentioned_ids or aid in seen:
                continue
            # Build the set of spellings that resolve to this aid.
            tokens = {aid.lower()}
            name = (forge_identity.get_identity(aid).get("name") or "").strip()
            if name:
                tokens.add(name.lower())
            for tok in list(tokens):
                if not tok or len(tok) < 2:
                    continue
                hit = False
                for m in re.finditer(re.escape(tok), lower_reply):
                    # Skip occurrences that are part of an @-mention
                    # (extract_mentions already saw those and they'd be
                    # in mentioned_ids if valid; if invalid like
                    # "@ [from: alice]" we don't want to double-warn).
                    prefix_idx = m.start() - 1
                    while prefix_idx >= 0 and reply[prefix_idx] in " \t":
                        prefix_idx -= 1
                    if prefix_idx >= 0 and reply[prefix_idx] == "@":
                        continue
                    start = max(0, m.start() - _HANDOFF_WINDOW)
                    end = min(len(reply), m.end() + _HANDOFF_WINDOW)
                    if _HANDOFF_VERB_RE.search(reply[start:end]):
                        hit = True
                        break
                if hit:
                    seen.add(aid)
                    misses.append(aid)
                    break
        return misses
    except Exception:
        return []


# ─── plan-without-action detection (2026-05-26 PR-16-followup) ──────
# Real router-visible incident: judy wrote "现在我直接开干前端…" then the
# turn ended with zero tool calls; Scott had to chase her. Same shape as
# the handoff-without-@ bug one layer up — agent describes work instead
# of doing it.
_PLAN_INTENT_RE = re.compile(
    "现在我直接|现在我去|接下来我|我马上|我立刻|我去写|我去提|我去开|"
    "我会去|我会写|我会提|我这就去|我现在开干|我现在上手"
)
# Delivery markers — if present, the agent is reporting completed work,
# so a plan-intent phrase in the same reply is a recap, not a promise.
_DELIVERY_RE = re.compile(
    "已 commit|已 push|已 merge|已部署|已 PR|已开了 PR|已完成|完成了|"
    "搞定了|搞定。|搞定，|跑起来了|已 force-push|push 完|commit 完|"
    "已 rebase|已动手|已 self-merge|已 merged|交付完|发出去了|"
    r"PR #\d+|merged|squash-merged|已在 main|已 land|交去了|"
    r"代码在 #\d+"
)


def _detect_plan_without_action(reply: str) -> str | None:
    """Return the matched promise phrase if the reply looks like a plan
    without delivery markers; None otherwise.

    HEURISTIC. Bias: under-detect. Any delivery marker suppresses the
    warning even when a plan phrase is also present (most likely the
    agent did the work AND described next steps).
    """
    try:
        if not reply or len(reply) < 30:
            return None
        m = _PLAN_INTENT_RE.search(reply)
        if not m:
            return None
        if _DELIVERY_RE.search(reply):
            return None
        return m.group(0)
    except Exception:
        return None


# ─── public api ──────────────────────────────────────────────────────
def enqueue_if_needed(thread_id: str, post: dict[str, Any]) -> bool:
    """Inspect a freshly-added post and dispatch routing workers if applicable.

    Returns True if at least one agent route was dispatched. Safe to call
    from the HTTP handler: each (thread, agent) pair runs on its own daemon
    thread, bounded by MAX_PARALLEL_ROUTES.
    """
    if not post:
        return False
    if _draining:
        return False  # forge is shutting down; surfaced as orphan on next boot
    speaker = (post.get("speaker") or "").strip().lower()
    # V1.1: any non-router speaker can trigger routing. Was previously
    # scott-only, which directly contradicted the PRD chair-dispatch
    # decision tree (Scott 2026-05-24: a chair @ing a member did nothing).
    # Self-routing is filtered below so an agent can't accidentally @
    # themselves into a loop; cross-agent loops (A @ B → B @ A) are
    # still possible — we rely on (1) agents being instructed not to
    # @ each other ping-pong and (2) Scott closing pathological threads.
    if not speaker or speaker == ROUTER_SPEAKER_FALLBACK.lower():
        return False
    mentions = list(post.get("mentions") or [])
    # V1.2 (Scott 2026-05-24 21:22): mentions may be display names
    # ('Dora', '小巴', 'Buffett') instead of agent ids. Resolve each
    # token through forge_identity; if it doesn't resolve to a known
    # employee, pass the raw token through (downstream filtering still
    # drops the unknowns silently via Unknown-agent-id router errors,
    # but legit special tokens like 'chair' keep flowing).
    mentions = [_resolve_name_or_keep(m) for m in mentions]
    # V1.1: resolve the special @chair token to the actual chair of the
    # thread's squad. PRD-v1.0 §2 treats 'chair' as a role, not an agent id.
    # Doing this here (before dedupe) means @chair + @<chair_name> in the
    # same post collapses to one route, which is what users expect.
    if mentions and any((m or "").strip().lower() == "chair" for m in mentions):
        mentions = _resolve_chair_token(thread_id, mentions)
    # Implicit-mention chain (scott only, in order):
    #   1. parent_post_id → author of parent post (Slack-style reply)
    #   2. no parent either → thread's squad chair (V1.1, Scott 2026-05-24:
    #      "no @mention → default @chair")
    # Both stay scott-only on purpose; extending implicit-routing to agent
    # speakers would cascade pings in any reply chain.
    if not mentions and speaker == "scott":
        implicit = _implicit_mention_from_parent(thread_id, post.get("parent_post_id"))
        if implicit:
            mentions = [implicit]
        else:
            chair = _chair_for_thread(thread_id)
            if chair:
                mentions = [chair]
    if not mentions:
        return False

    # dedupe mentions while preserving order; also drop self-mentions
    # (an agent can't wake themselves up by @ing their own name) and the
    # reserved 'scott' token (scott is the human, not an agent endpoint).
    seen: set[str] = set()
    ordered: list[str] = []
    for m in mentions:
        key = (m or "").strip().lower()
        if not key or key in seen:
            continue
        if key == speaker:
            continue  # self-@ no-op
        if key in _RESERVED_SPEAKERS:
            continue  # @scott / @__router__ are not routable
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


def _resolve_name_or_keep(raw: str) -> str:
    """Map an @-mention spelling to its canonical agent id when possible.

    'Dora' → 'designer', 'designer' → 'designer', '小巴' → 'xiaoba',
    'chair' → 'chair' (special token, _resolve_chair_token handles it),
    'foo' → 'foo' (unknown; downstream produces an Unknown-agent-id
    error post so misspellings are visible rather than silently dropped).
    """
    if not raw:
        return raw
    # Preserve special tokens that are NOT employee names — chair token
    # has its own resolver below.
    if raw.strip().lower() == "chair":
        return raw
    resolved = forge_identity.name_to_id(raw)
    return resolved if resolved else raw


def _chair_for_thread(thread_id: str) -> str | None:
    """Resolve the chair agent id of `thread_id`'s squad, or None if it
    can't be determined (thread/squad missing, chair unset, or chair is
    the literal string 'chair' — same recursion guard as the @chair
    token resolver).
    """
    try:
        thread = store.project_thread(thread_id)
        squad_id = (thread or {}).get("squad_id")
        if not squad_id:
            return None
        squad = store.get_squad(squad_id)
        ch = ((squad or {}).get("chair") or "").strip().lower()
        if ch and ch != "chair":
            return ch
    except Exception:
        return None
    return None


def _resolve_chair_token(thread_id: str, mentions: list[str]) -> list[str]:
    """Replace any occurrence of 'chair' in `mentions` with the actual
    chair of the thread's squad. If we can't resolve (thread gone, squad
    gone, no chair set), drop the 'chair' token entirely — better than
    routing to a nonexistent agent and getting the ugly
    'Unknown agent id "chair"' router-error post.
    """
    chair_id: str | None = None
    try:
        thread = store.project_thread(thread_id)
        squad_id = (thread or {}).get("squad_id")
        if squad_id:
            squad = store.get_squad(squad_id)
            ch = (squad or {}).get("chair") or ""
            ch = ch.strip().lower()
            if ch and ch != "chair":  # guard against pathological recursion
                chair_id = ch
    except Exception:
        chair_id = None
    out: list[str] = []
    for m in mentions:
        if (m or "").strip().lower() == "chair":
            if chair_id:
                out.append(chair_id)
            # else: drop silently (downstream dedupe handles empties)
        else:
            out.append(m)
    return out


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



def _dispatch(thread_id: str, agent_id: str, trigger_pid: str,
              chip_post_id: str | None = None) -> bool:
    """Spawn a worker thread for one (thread, agent) pair unless already running."""
    key = (thread_id, agent_id)
    with _inflight_lock:
        if key in _inflight:
            return False  # drop the duplicate silently
        _inflight.add(key)
    t = threading.Thread(
        target=_run_one,
        args=(thread_id, agent_id, trigger_pid, chip_post_id),
        name=f"openforge-route-{agent_id}-{thread_id[-6:]}",
        daemon=True,
    )
    t.start()
    return True


def _run_one(thread_id: str, agent_id: str, trigger_pid: str,
             chip_post_id: str | None = None) -> None:
    """Worker entry: bounded-parallel agent invocation for a single mention."""
    key = (thread_id, agent_id)
    # Bound total in-flight subprocesses. (thread, agent) dedupe is enforced
    # BEFORE we wait on the semaphore so duplicates don't pile up in the
    # slot queue.
    try:
        with _slots:
            try:
                if chip_post_id is None:
                    _route_to_agent_safely(thread_id, agent_id, trigger_pid)
                else:
                    _route_to_agent_safely(thread_id, agent_id, trigger_pid, chip_post_id)
            except Exception as e:
                _record_crash(thread_id, agent_id, e, chip_post_id=chip_post_id)
    finally:
        with _inflight_lock:
            _inflight.discard(key)


def _record_crash(thread_id: str, agent_id: str, e: BaseException,
                  chip_post_id: str | None = None) -> None:
    try:
        if chip_post_id:
            _patch_chip(thread_id, chip_post_id, phase="failed", error=_error_tail(e))
        store.write_thread_markdown(thread_id)
    except Exception:
        pass


# ─── core routing ────────────────────────────────────────────────────
def _route_to_agent_safely(thread_id: str, agent_id: str, trigger_pid: str,
                           chip_post_id: str | None = None) -> None:
    thread = store.project_thread(thread_id)
    if thread is None:
        return
    if thread.get("closed_at"):
        if chip_post_id:
            _patch_chip(thread_id, chip_post_id, phase="skipped")
        return  # closed mid-flight; skip silently
    trigger = _find_trigger_post(thread, trigger_pid)
    if not trigger:
        if chip_post_id:
            _patch_chip(thread_id, chip_post_id, phase="skipped")
        return
    # We intentionally do NOT re-check `agent_id in trigger.mentions` here:
    # the dispatcher (enqueue_if_needed) is the single source of truth for
    # who to route to (it also resolves implicit-mention-via-reply), and
    # re-deriving here would drop those.
    try:
        _route_to_agent(thread_id, agent_id, trigger, chip_post_id=chip_post_id)
    finally:
        try:
            store.write_thread_markdown(thread_id)
        except Exception:
            pass


def _route_to_agent(thread_id: str, agent_id: str, trigger: dict,
                    chip_post_id: str | None = None) -> None:
    """Run one agent turn → append reply. Concurrency-safe."""
    session_id = f"forge-{thread_id}-{agent_id}"
    trigger_pid = trigger.get("post_id") or trigger.get("id")
    # Step 1: announce we are working so the UI shows progress immediately.
    started = time.monotonic()
    placeholder_id: str | None = chip_post_id
    try:
        if placeholder_id:
            _patch_chip(thread_id, placeholder_id, phase="thinking", content=_chip_content(agent_id))
        else:
            ph = store.add_thread_post(
                thread_id, ROUTER_SPEAKER_FALLBACK,
                _chip_content(agent_id),
                parent_post_id=trigger_pid,
                post_type="status_chip",
                phase="thinking",
                trigger_post_id=trigger_pid,
                agent_id=agent_id,
            )
            placeholder_id = ph.get("post_id")
        store.write_thread_markdown(thread_id)
    except Exception:
        pass

    snap = snapshot_main(agent_id)
    final_post_id: str | None = None
    try:
        prompt = _build_prompt(thread_id, agent_id, trigger)
        # PR-B2: inject OPENFORGE_PROJECT_DIR into the spawned agent's env
        # iff the current squad has a configured AND valid project_dir.
        # The script in PR-C1 (openforge-worktree) reads this env var to
        # locate the target repo without the agent needing to know the path.
        # Encapsulation principle (PRD §5.3): agent never sees the value.
        spawn_env = _spawn_env_for_thread(thread_id)
        try:
            if placeholder_id:
                _patch_chip(thread_id, placeholder_id, phase="running")
            if agent_id in forge_employees.acp_employee_ids():
                prompt = _render_acp_preamble(thread_id, agent_id, trigger) + prompt
            reply = call_agent(agent_id, session_id, prompt, extra_env=spawn_env)
        except AgentError as e:
            if placeholder_id:
                _patch_chip(thread_id, placeholder_id,
                            phase="failed", error=_error_tail(e),
                            duration_ms=_duration_ms(started))
            return
        reply = clean(reply)
        if is_empty(reply):
            if placeholder_id:
                _patch_chip(thread_id, placeholder_id,
                            phase="skipped", duration_ms=_duration_ms(started))
            return
        added = store.add_thread_post(
            thread_id, agent_id, reply, parent_post_id=trigger_pid,
            from_chip_post_id=placeholder_id,
        )
        final_post_id = added.get("post_id")
        # 2026-05-26: defensive handoff-mention check. If the agent
        # wrote prose like "alice 可以动了" / "交给 milk" but did NOT
        # @ them, the named teammate will not be woken by the router
        # and the thread silently stalls. Post a __router__ hint so
        # the human (and the next agent reading the thread) can see
        # exactly which handoff was dropped and how to fix it.
        try:
            extracted = store.extract_mentions(reply)
            misses = _detect_missing_handoff_mentions(reply, extracted, agent_id)
            for missed_id in misses:
                display = forge_identity.get_identity(missed_id).get(
                    "name"
                ) or missed_id
                store.add_thread_post(
                    thread_id, ROUTER_SPEAKER_FALLBACK,
                    f"💡 检测到 @{forge_identity.get_identity(agent_id)['name']} "
                    f"点名了 **{display}** 作为下一步的人，但没有 @{missed_id}。"
                    f"{display} 不会收到通知，thread 会在这里冻住。"
                    f"如需 {display} 接力，请回复 `@{missed_id} <内容>`。",
                    parent_post_id=final_post_id,
                )
        except Exception as e:
            print(f"⚠️  handoff-mention hint failed: {e!r}", flush=True)
        # 2026-05-26 PR-16-followup: plan-without-action detector.
        # Catches replies like "现在我直接开干前端" / "我马上去 commit"
        # that promise future work but contain no delivery markers, so
        # the agent ends the turn without actually doing anything and
        # Scott has to chase. Hints, never blocks.
        try:
            plan_phrase = _detect_plan_without_action(reply)
            if plan_phrase:
                display = forge_identity.get_identity(agent_id).get(
                    "name"
                ) or agent_id
                store.add_thread_post(
                    thread_id, ROUTER_SPEAKER_FALLBACK,
                    f"💡 检测到 @{display} 写了「{plan_phrase}…」但本 turn 没有交付标记"
                    f"（已 commit / PR 已开 / push 完 等）。计划不是交付；下一条 trigger 到之前 "
                    f"thread 不会自动推进。如果你是能做这件事的人，请直接调工具干完再回复。",
                    parent_post_id=final_post_id,
                )
        except Exception as e:
            print(f"⚠️  plan-without-action hint failed: {e!r}", flush=True)
        if placeholder_id:
            _patch_chip(thread_id, placeholder_id,
                        phase="done", duration_ms=_duration_ms(started))
        # V1.1 (Scott 2026-05-24 21:00): re-feed the agent's own reply
        # through the router so @mentions inside agent→agent dispatch
        # actually wake their targets. Without this, a chair like judy
        # writing '@designer @alice please look' got the post added but
        # no router event fired — dead ping. The HTTP POST /posts path
        # already does this; we missed mirroring it on the agent-reply
        # path. Best-effort: any router error here is logged, not raised,
        # so an in-flight reply still lands cleanly.
        try:
            post_router_view = {
                "post_id": final_post_id,
                "speaker": agent_id,
                "content": reply,
                "mentions": store.extract_mentions(reply),
                "parent_post_id": trigger_pid,
            }
            enqueue_if_needed(thread_id, post_router_view)
        except Exception as e:
            print(f"⚠️  agent-reply re-enqueue failed: {e!r}", flush=True)
    except Exception as e:
        if placeholder_id:
            try:
                _patch_chip(thread_id, placeholder_id,
                            phase="failed", error=_error_tail(e),
                            duration_ms=_duration_ms(started))
            except Exception:
                pass
        return
    finally:
        if snap:
            try:
                restore_main(agent_id, snap)
            except Exception:
                pass


def _chip_content(agent_id: str) -> str:
    return f"{agent_id} thinking"


def _duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _error_tail(e: BaseException) -> str:
    return str(e)[-ERROR_TAIL_LIMIT:]


def _patch_chip(thread_id: str, post_id: str, **patch: Any) -> dict:
    clean_patch = {k: v for k, v in patch.items() if v is not None}
    return store.patch_post(thread_id, post_id, clean_patch)


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
    # Filter out HTML comments AND rewrite each post header from
    #   "#### <speaker> · <time>"
    # to
    #   "#### [from: <speaker>] · <time>"
    # The structured `from:` tag is a much stronger attention cue than a
    # bare display name when there are look-alike agent ids in play
    # (milk/miki, alice/alex, sherry/cherry, …). Without it LLMs have been
    # caught addressing the wrong teammate in their reply (real incident,
    # 2026-05-25).
    md_lines = []
    for l in md.splitlines():
        if l.startswith("<!--"):
            continue
        if l.startswith("#### ") and " · " in l:
            try:
                head, rest = l[5:].split(" · ", 1)
                l = f"#### [from: {head.strip()}] · {rest}"
            except Exception:
                pass
        md_lines.append(l)
    md_for_prompt = "\n".join(md_lines)
    trigger_preview = (trigger.get("content") or "").strip()
    trigger_speaker = (trigger.get("speaker") or "").strip() or "unknown"

    # v0.9: prepend OpenForge-collected context bundle so the spawned
    # subprocess starts with situational awareness (STATUS + main session
    # tail + memory hits) and doesn't redo work the main session already did.
    bundle_preamble = _render_bundle_preamble(thread_id, agent_id, trigger_preview)

    return (
        f"{bundle_preamble}"
        f"你正在参加 OpenForge 一个 thread 的讨论，**{trigger_speaker}** 在最新一条 post "
        f"里 @ 了你（{agent_id}）。下面是 thread 当前的完整内容：\n\n"
        f"━━━ Thread ━━━\n{md_for_prompt}\n━━━ 结束 ━━━\n\n"
        f"[你的身份]: {agent_id}\n"
        f"[触发这次 turn 的 post — from: {trigger_speaker}]:\n{trigger_preview}\n\n"
        f"要求：\n"
        f"- 用中文，简洁段落（不要 markdown 标题）\n"
        f"- 你的回复会被独立保存为这条 thread 的一条新 post\n"
        f"- 直接针对 **{trigger_speaker}** 的最新 post 回应，不要复述之前的内容\n"
        f"- **核对 teammate 名字**：阅读 thread 时，每条 post 头部的 `[from: <id>]` "
        f"是该 post 的发言人；要在自己的回复里提到某人，先去那里核对 id 拼写。"
        f"milk/miki、alice/alex、sherry/cherry 这类相似名字必须 double-check；"
        f"写错名字会污染 thread 信任。\n"
        f"- **@ mention 的正确写法是 `@<id>`**（例如 `@alice`、`@milk`）。"
        f"`[from: <id>]` 只是 thread 给你看的标记，**不要**写成 `@ [from: alice]` "
        f"或 `@[from:alice]` —— router 解析不了这种语法，被你 @ 的人收不到通知。\n"
        f"- **交接必须 @**：如果你的回复里点名某个 teammate 是“下一步的人”—— 要她接力、"
        f"看东西、决定、干活——必须在名字前面写 `@<id>` 才能唤醒 router。"
        f"只在散文里写「alice 可以动了」或「交给 milk」**不会**让对方收到通知，"
        f"thread 会完全冻在那里。例子：\n"
        f"  ❌  “alice review 完了可以动了” — alice 不会被唤醒\n"
        f"  ✅  “@alice 你 review 完了就可以动” — alice 会收到 trigger\n"
        f"- **你能做的事，自己做完再回复**。如果 trigger 是「让你做 X」或你是 squad 里能做 X "
        f"的人，**不要**在回复里写「接下来我会做 / 现在我去 @ 别人做 / 马上去写」。"
        f"直接调工具把它做完（exec / edit / write / openforge-worktree / git…），"
        f"然后 post 里只汇报**结果**——「已 commit / PR 已开 / 已 push / 已部署」。"
        f"计划不是交付，只有动作是交付。例外只有两种："
        f"(1) 需要别人的专门能力（如 design 找 dora）且你这边没任何能做的部分；"
        f"(2) scott 明确要的就是「听取你的判断」（你怎么看 / 评估一下 / 让你决定）。\n"
        f"  ❌  「现在我直接开干前端：照搬 mock CSS 进 web/style.css、6 分钟写主体、5 分钟跨浏览器验。」 (这是计划)\n"
        f"  ✅  「已 commit `abc1234`，PR #17 已开 https://github.com/...。」 (这是交付)\n"
        f"- 如果问题已经被回答过或不需要你回答，回复 `completed`\n"
        f"\n"
        f"[文件引用语法 v0.8]\n"
        f"方案 A（推荐）— 你在自己 workspace 生成了文件，希望 scott 能点开看：\n"
        f"  1) 先把文件写到你自己的 workspace（你一直在这么做）\n"
        f"  2) 注册到 OpenForge（**在 thread 里发言时务必带 `thread_id`，否则 Favorites 列表的来源 thread meta 会显示为「—」**）：\n"
        f"     curl -sX POST http://127.0.0.1:7878/api/refs -H 'Content-Type: application/json' \\\n"
        f"       -d '{{\"label\":\"<filename>\",\"abs_path\":\"<绝对路径>\",\"source_agent\":\"{agent_id}\",\"thread_id\":\"{thread_id}\"}}'\n"
        f"  3) 回复里引用： `[[{agent_id}/<filename>]]`（推荐）、`[[ref:<id>]]`、或 `[[<filename>]]`\n"
        f"     同一个文件重复注册会返回相同 id（幂等），abs_path 必须是绝对路径。\n"
        f"\n方案 B（v0.7 兼容）— 如果文件已经在配置的 fileRoots 里：\n"
        f"- `[[name.md]]`              → 默认根目录下的文件\n"
        f"- `[[docs/PRD.md]]`          → 指定 root id\n"
        f"- `[[docs/PRD.md|这份 PRD]]` → 自定义显示文本\n"
        f"前端会自动渲染为 📄 可点 chip；不要手动拼 URL，不要凭空推测文件名。\n"
        f"\n[你的 STATUS.md 维护]\n"
        f"当前你的 STATUS 会被 OpenForge 自动注入到你的上下文里（看到上面的 STATUS 区块了吗？）。\n"
        f"以下场景主动更新：\n"
        f"- 完成一个阶段性任务 / 进入新任务 / 袁 blocker\n"
        f"更新方式（在你的 reply 中静默执行，不要跳出来告诉 scott）：\n"
        f"  curl -sX PATCH http://127.0.0.1:7878/api/agents/{agent_id}/status \\\n"
        f"    -H 'Content-Type: application/json' \\\n"
        f"    -d '{{\"section\":\"当前焦点\",\"content\":\"...一句话...\"}}'\n"
        f"或者全量重写：POST 同路径 body `{{\"content\":\"...完整 STATUS.md...\"}}`。\n"
        f"\n[需要历史细节？主动查 memory]\n"
        f"上面这份 context bundle **不会**预查 memory（设计哲学：memory 是 ask-on-demand 仓库）。\n"
        f"需要以前的决定 / 讨论 / 历史事件时，主动调你自己的 OpenClaw 工具：\n"
        f"  memory_search(query=\"...\")\n"
        f"未查就凭印象回答 = 黑线。\n"
    )


def _render_acp_preamble(thread_id: str, agent_id: str, trigger: dict) -> str:
    """Render identity/context for ACP CLI employees.

    ACP CLIs are not OpenClaw agents, so they do not automatically receive
    workspace identity. Keep the block conditional and compact.
    """
    trigger_text = (trigger.get("content") or "").strip()
    trigger_speaker = (trigger.get("speaker") or "").strip() or "unknown"
    parts = [
        "[OpenForge ACP employee preamble]",
        f"你的身份: {agent_id} ACP CLI employee",
        f"thread_id: {thread_id}",
    ]
    soul_path = Path.home() / ".openclaw" / f"workspace-{agent_id}" / "SOUL.md"
    try:
        if soul_path.exists() and soul_path.is_file():
            soul = soul_path.read_text(encoding="utf-8").strip()
            if soul:
                parts.extend(["", "[已注册 SOUL.md]", soul])
    except Exception:
        pass
    parts.extend([
        "",
        f"[触发 post 原文 — from: {trigger_speaker}]",
        trigger_text,
        "[/OpenForge ACP employee preamble]",
        "",
    ])
    return "\n".join(parts)


def _render_bundle_preamble(thread_id: str, agent_id: str, trigger_preview: str) -> str:
    """Build and render the v0.9 context bundle as a prompt preamble.

    Fails soft: if forge_context isn't importable or bundle building raises,
    returns "" so the agent still gets a usable prompt.

    **Design principle (PR-B1, codified for future contributors):**
    Preamble segments are *conditionally injected* — if there is no data to
    convey, the segment is omitted entirely. The visibility of a segment is
    itself a signal: e.g. the ``[project]`` block only appears when the
    current squad has a configured project_dir, so its presence tells the
    agent "this is a development squad". When you add new metadata
    segments, never render an empty/placeholder block "for consistency";
    that pollutes agent context. No data → don't show.
    """
    if forge_context is None:
        return ""
    try:
        thread = store.project_thread(thread_id) or {}
        title = (thread.get("posts") or [{}])[0].get("content") or trigger_preview
        query_hint = (title or "").strip()[:200]
        bundle = forge_context.build_context_bundle(
            agent_id, query_hint=query_hint,
        )
        rendered = bundle.render()
    except Exception:
        return ""
    project_section = _render_project_section(thread_id)
    if not rendered and not project_section:
        return ""
    head = "## 你的最新上下文（OpenForge 已预查，请基于此回复）\n\n"
    body_parts: list[str] = []
    if project_section:
        body_parts.append(project_section)
    if rendered:
        body_parts.append(rendered)
    return head + "\n\n".join(body_parts) + "\n\n---\n\n"


def _spawn_env_for_thread(thread_id: str) -> dict[str, str] | None:
    """Return env vars to inject into the spawned agent subprocess.

    Currently returns ``{'OPENFORGE_PROJECT_DIR': <path>}`` when the
    thread's squad has a configured project_dir that passes fs validation,
    or ``None`` otherwise. PR-B2; consumed by PR-C1's openforge-worktree.

    Invariant: only inject when the value is *currently valid*, so the
    worktree helper never sees a stale/typo'd path. If config drifts after
    spawn the agent's [project] preamble warning (PR-B1) is the user-facing
    signal; the env var is for the script, not the human.
    """
    try:
        thread = store.project_thread(thread_id) or {}
        squad_id = thread.get("squad_id")
        if not squad_id:
            return None
        squad = store.get_squad(squad_id) or {}
        project_dir = squad.get("project_dir")
    except Exception:
        return None
    if not project_dir:
        return None
    if not forge_project.derive_validity(project_dir):
        return None
    return {"OPENFORGE_PROJECT_DIR": project_dir}


def _render_project_section(thread_id: str) -> str:
    """Render the ``[project]`` preamble segment for a given thread.

    Three behaviors, driven by ``squad.project_dir`` + a live filesystem check:

    * **unset** — returns ``""`` (segment omitted; visibility = signal).
    * **set + valid (path exists AND is a git repo)** — returns a short OK
      banner. PR-B2 will additionally inject ``OPENFORGE_PROJECT_DIR`` into
      the spawned subprocess env; PR-C2 will add the worktree rule preamble
      gated on the same condition. The banner deliberately does NOT include
      the path itself — agents don't need to know it (encapsulation; PRD
      §5.3). The path lives in env / scripts; the prompt only says "locked".
    * **set + invalid (path missing or not a git repo)** — returns a loud
      warning so agents see config drift early and don't waste a turn
      writing into a broken target.
    """
    try:
        thread = store.project_thread(thread_id) or {}
        squad_id = thread.get("squad_id")
        if not squad_id:
            return ""
        squad = store.get_squad(squad_id) or {}
        project_dir = squad.get("project_dir")
    except Exception:
        return ""
    if not project_dir:
        return ""
    try:
        v = forge_project.validate(project_dir)
    except Exception:
        # If validation itself bombs, prefer the warning path over silently
        # injecting the OK banner — fail loud.
        v = {"exists": False, "is_git_repo": False, "error": "validate raised"}
    if v.get("exists") and v.get("is_git_repo"):
        return (
            "[project]\n"
            "目标 repo 已由当前 squad 锁定。记录在 squad 配置里，调用脚本会自动拿到路径。\n"
            "\n"
            "[代码改动规则]\n"
            "修改这个 squad 的目标 repo 时，按以下步骤——不要直接在主 repo 改：\n"
            "1. `openforge-worktree add <你的 agent id> <task-slug>` — 输出即 worktree 绝对路径，cd 进去。\n"
            "2. 在该路径下 edit / commit / `git push -u origin <agent>/<task-slug>`。\n"
            "3. `gh pr create`，标题以 `[<agent>] ` 开头，body 贴当前 thread 链接。\n"
            "4. PR merge 后：`openforge-worktree rm <agent>/<task-slug>`。\n"
            "例外（不要建 worktree）：只读看代码、文档写在自己 workspace、scott 明确说「直接改主 repo」。\n"
            "不要传 `--repo` 或猜路径。"
        )
    return (
        "[project] ⚠️ 配置异常\n"
        f"  squad.project_dir = {project_dir}\n"
        "  问题：路径不存在 或 不是 git 仓库。\n"
        "  worktree 规则本轮已禁用；如要动代码，先改 squad 配置后重试。"
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


# ─── orphan placeholder recovery ─────────────────────────────────────
# Placeholder content shape produced by _route_to_agent's step 1
# announce. We match the prefix only; the agent display-name varies.
_PLACEHOLDER_PREFIX = "⏳ @"
_PLACEHOLDER_SUFFIX = "正在思考中…"


def _looks_like_placeholder(content: str) -> bool:
    s = (content or "").strip()
    return s.startswith(_PLACEHOLDER_PREFIX) and _PLACEHOLDER_SUFFIX in s


def _placeholder_target_id(content: str) -> str | None:
    """Recover the canonical agent id from `⏳ @<DisplayName> 正在思考中…`.

    Returns None if the name doesn't resolve to a known employee.
    """
    s = (content or "").strip()
    if not _looks_like_placeholder(s):
        return None
    # strip prefix '⏳ @' and the trailing suffix
    body = s[len(_PLACEHOLDER_PREFIX):]
    name = body.split("正在思考中")[0].strip()
    if not name:
        return None
    return forge_identity.name_to_id(name)


def recover_orphan_placeholders(redispatch: bool = False) -> list[dict]:
    """Sweep all open threads for `__router__` placeholders left dangling.

    A placeholder is "orphan" if the projection shows it as still NOT
    superseded — meaning the worker that posted it never finished (most
    commonly because the server was restarted mid-turn and the in-flight
    set is process-local memory that doesn't survive restart).

    For each orphan we **always** supersede the placeholder with a
    `__router__` interrupt note so the UI's `⏳ @X 正在思考中…` indicator
    clears.

    `redispatch` defaults to **False** as of 2026-05-26 PR-17-followup.
    Auto-redispatch caused EmbeddedAttemptSessionTakeoverError because
    the previous worker held an advisory lock on the agent's session
    JSONL that wasn't released by the time the new worker re-opened it.
    Beyond the race, blindly re-firing an old trigger after a restart
    is often the wrong thing — the world may have changed (code, deps,
    Scott's intent). Pass `redispatch=True` to opt back in for tests
    or controlled tooling.

    When `redispatch=False`, the interrupt note tells Scott to re-@
    manually if the work still matters. One human keystroke, zero
    correctness risk.

    Returns a list of {thread_id, agent_id, trigger_pid, redispatched}
    records, suitable for printing on the boot banner.

    Safe to call from server startup before serve_forever(); failures on
    individual threads are isolated and never raised.
    """
    out: list[dict] = []
    try:
        thread_ids = store.list_thread_ids()
    except Exception:
        return out
    for tid in thread_ids:
        try:
            t = store.project_thread(tid)
        except Exception:
            continue
        if not t:
            continue
        if t.get("closed_at"):
            continue
        for post in t.get("posts") or []:
            if post.get("superseded"):
                continue
            if (post.get("speaker") or "").strip() != ROUTER_SPEAKER_FALLBACK:
                continue
            content = post.get("content") or ""
            if not _looks_like_placeholder(content):
                continue
            placeholder_id = post.get("post_id") or post.get("id")
            trigger_pid = post.get("parent_post_id")
            agent_id = _placeholder_target_id(content)
            if not agent_id:
                # Name didn't resolve — supersede anyway so the UI clears.
                try:
                    note = store.add_thread_post(
                        tid, ROUTER_SPEAKER_FALLBACK,
                        f"⚠️ 上一条 turn 在 server 重启时被中断（无法识别 agent 名字「{content}」，"
                        "请人工 @ 一次重试）。",
                        parent_post_id=trigger_pid,
                    )
                    store.supersede_thread_post(
                        tid, placeholder_id, by_post_id=note.get("post_id"),
                    )
                    store.write_thread_markdown(tid)
                except Exception:
                    pass
                out.append({
                    "thread_id": tid, "agent_id": None,
                    "trigger_pid": trigger_pid, "redispatched": False,
                })
                continue
            # Compose the replacement note + supersede the placeholder.
            try:
                display = (forge_identity.get_identity(agent_id) or {}).get(
                    "name"
                ) or agent_id
                if redispatch:
                    note_text = (
                        f"⚠️ @{display} 上一条 turn 在 server 重启时被中断，"
                        "正在重新触发…"
                    )
                else:
                    note_text = (
                        f"⚠️ @{display} 上一条 turn 在 server 重启时被中断。"
                        "如果还需要这件事，请人工 @ 一次重新触发；"
                        "router 不会自动重发以避免 session 文件竞态。"
                    )
                note = store.add_thread_post(
                    tid, ROUTER_SPEAKER_FALLBACK,
                    note_text,
                    parent_post_id=trigger_pid,
                )
                store.supersede_thread_post(
                    tid, placeholder_id, by_post_id=note.get("post_id"),
                )
                store.write_thread_markdown(tid)
            except Exception:
                pass
            # Re-dispatch only if we still have the original trigger.
            did_redispatch = False
            if redispatch and trigger_pid:
                try:
                    did_redispatch = _dispatch(tid, agent_id, trigger_pid)
                except Exception:
                    did_redispatch = False
            out.append({
                "thread_id": tid, "agent_id": agent_id,
                "trigger_pid": trigger_pid, "redispatched": did_redispatch,
            })
    return out
