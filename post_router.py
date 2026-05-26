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
import time
from typing import Any

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
            f"⚠️ post router crashed routing @{forge_identity.get_identity(agent_id)['name']}: {e!r}",
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
            f"⏳ @{forge_identity.get_identity(agent_id)['name']} 正在思考中…",
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
                f"⚠️ @{forge_identity.get_identity(agent_id)['name']} 没回复: {e}",
                parent_post_id=trigger_pid,
            )
            final_post_id = err.get("post_id")
            return
        reply = clean(reply)
        if is_empty(reply):
            err = store.add_thread_post(
                thread_id, ROUTER_SPEAKER_FALLBACK,
                f"_(@{forge_identity.get_identity(agent_id)['name']} 返回空回复)_",
                parent_post_id=trigger_pid,
            )
            final_post_id = err.get("post_id")
            return
        added = store.add_thread_post(
            thread_id, agent_id, reply, parent_post_id=trigger_pid,
        )
        final_post_id = added.get("post_id")
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
        f"- **引用任何 teammate 时必须用 thread 里出现的 `[from: <id>]` 字段为准**，"
        f"不要靠记忆/语感拼名字。milk/miki、alice/alex、sherry/cherry 这类相似名字"
        f"必须 double-check 当前 post header；写错名字会污染 thread 信任。\n"
        f"- 如果问题已经被回答过或不需要你回答，回复 `completed`\n"
        f"\n"
        f"[文件引用语法 v0.8]\n"
        f"方案 A（推荐）— 你在自己 workspace 生成了文件，希望 scott 能点开看：\n"
        f"  1) 先把文件写到你自己的 workspace（你一直在这么做）\n"
        f"  2) 注册到 OpenForge：\n"
        f"     curl -sX POST http://127.0.0.1:7878/api/refs -H 'Content-Type: application/json' \\\n"
        f"       -d '{{\"label\":\"<filename>\",\"abs_path\":\"<绝对路径>\",\"source_agent\":\"{agent_id}\"}}'\n"
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
            "目标 repo 已由当前 squad 锁定。记录在 squad 配置里，调用脚本会自动拿到路径。"
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
