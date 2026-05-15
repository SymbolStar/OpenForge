#!/usr/bin/env python3
"""
run_standup.py v0.3 — JSONL-backed multi-agent morning standup.

Source of truth: ~/.openclaw/standups/data/<date>/events.jsonl
Markdown view:   ~/.openclaw/standups/standup-<date>.md  (regenerated)

Key v0.3 changes vs v0.2:
- All state goes through forge_store (events.jsonl + fcntl lock)
- subprocess returncode is checked; failed CLI calls do not silently fallback
- Single-flight: refuses to run a second standup for the same date concurrently
- Markdown is a derived view, regenerated atomically after every event
- Per-agent isolated session preserved: standup-<date>-<agent>
"""

from __future__ import annotations

import argparse
import atexit
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# allow running as a script or as a module
sys.path.insert(0, str(Path(__file__).parent))
import forge_store as store

AGENTS_ROOT = Path.home() / ".openclaw" / "agents"


# ─── main-session snapshot / restore ─────────────────────────────────
# `openclaw agent --session-id X` MUTATES agent:<id>:main to point at X.
# That hijacks the agent's primary chat session, polluting future user turns.
# We work around it by snapshotting each participant's main pointer before the
# meeting and restoring it afterward (best-effort, also via atexit).
#
# Snapshot is also written into events.jsonl so a stale crash can be recovered.

_RESTORE_REGISTERED = False
_PENDING_RESTORES: dict[str, dict] = {}  # agent_id -> snapshot


def _sessions_path(agent_id: str) -> Path:
    return AGENTS_ROOT / agent_id / "sessions" / "sessions.json"


def snapshot_main(agent_id: str) -> dict | None:
    p = _sessions_path(agent_id)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    main = d.get(f"agent:{agent_id}:main")
    if not isinstance(main, dict):
        return None
    return {
        "agent": agent_id,
        "sessionId": main.get("sessionId"),
        "sessionFile": main.get("sessionFile"),
        "snapshotAt": int(time.time() * 1000),
    }


def restore_main(agent_id: str, snapshot: dict) -> bool:
    p = _sessions_path(agent_id)
    if not p.exists() or not snapshot or not snapshot.get("sessionId"):
        return False
    try:
        d = json.loads(p.read_text())
    except Exception:
        return False
    key = f"agent:{agent_id}:main"
    main = d.get(key)
    if not isinstance(main, dict):
        return False
    # only restore if the current main was clobbered by us
    # (huddle- kept for backward compatibility with pre-rename runs)
    cur_sid = main.get("sessionId") or ""
    if not (cur_sid.startswith("standup-") or cur_sid.startswith("forge-") or cur_sid.startswith("huddle-")):
        return False
    main["sessionId"] = snapshot["sessionId"]
    main["sessionFile"] = snapshot["sessionFile"]
    main["updatedAt"] = int(time.time() * 1000)
    main["restoredFromOpenForge"] = True
    d[key] = main
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    os.replace(tmp, p)
    return True


def snapshot_participants(date: str, agents: list[str]) -> dict[str, dict]:
    snaps: dict[str, dict] = {}
    for ag in agents:
        s = snapshot_main(ag)
        if s:
            snaps[ag] = s
            _PENDING_RESTORES[ag] = s
    if snaps:
        # persist into events.jsonl so a hard crash is still recoverable.
        store.append_event(date, {
            "kind": "agent_main_snapshot",
            "snapshots": snaps,
        })
    _ensure_atexit_handler()
    return snaps


def restore_participants() -> int:
    n = 0
    for ag, snap in list(_PENDING_RESTORES.items()):
        try:
            if restore_main(ag, snap):
                n += 1
        except Exception as e:
            print(f"⚠️  restore failed for {ag}: {e}", file=sys.stderr)
        finally:
            _PENDING_RESTORES.pop(ag, None)
    return n


def _ensure_atexit_handler():
    global _RESTORE_REGISTERED
    if _RESTORE_REGISTERED:
        return
    _RESTORE_REGISTERED = True
    atexit.register(restore_participants)

# ─── config ───────────────────────────────────────────────────────────
DEFAULT_MEMBERS = ["milk", "sentry", "bugfix", "milly", "kb"]
DEFAULT_CHAIR = "milk"
PER_TOPIC_MAX_TURNS = 8
AGENT_TIMEOUT = 180

NOISE_PATTERNS = [
    re.compile(r"^\[plugins\].*$", re.MULTILINE),
    re.compile(r"^Config warnings:.*$", re.MULTILINE),
    re.compile(r"^- plugins\..*$", re.MULTILINE),
    re.compile(r"^🦞 OpenClaw.*$", re.MULTILINE),
]
EMPTY_MARKERS = {"completed", "", "_(空回复)_"}


def clean(text: str) -> str:
    out = text or ""
    for pat in NOISE_PATTERNS:
        out = pat.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def is_empty(text: str) -> bool:
    return clean(text).lower() in EMPTY_MARKERS


# ─── agent CLI bridge ─────────────────────────────────────────────────
class AgentError(RuntimeError):
    pass


def call_agent(agent_id: str, session_id: str, prompt: str) -> str:
    """Invoke `openclaw agent`. Raises AgentError on non-zero exit."""
    try:
        result = subprocess.run(
            [
                "openclaw", "agent",
                "--agent", agent_id,
                "--session-id", session_id,
                "--message", prompt,
            ],
            capture_output=True, text=True, timeout=AGENT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise AgentError(f"timeout after {AGENT_TIMEOUT}s")
    except FileNotFoundError:
        raise AgentError("`openclaw` CLI not found on PATH")

    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise AgentError(
            f"openclaw agent exited {result.returncode}: " + " | ".join(tail)
        )
    return clean(result.stdout)


# ─── prompt templates ─────────────────────────────────────────────────
def render_minutes_for_prompt(date: str) -> str:
    """The plain-text view of the meeting we feed back to each agent."""
    md = store.render_markdown(date)
    # strip the housekeeping header line so agents don't echo it
    return "\n".join(l for l in md.splitlines()
                     if not l.startswith("<!--")) or "(空)"


def build_topic_prompt(date: str, agent_id: str, agenda: str,
                       current_topic: str, role_hint: str) -> str:
    return f"""你正在参加多 agent 早会。这是会议纪要的当前内容（你能看到所有人之前的发言）：

━━━ 会议纪要 ━━━
{render_minutes_for_prompt(date)}
━━━ 纪要结束 ━━━

[你的身份]: {agent_id}
[今日议程]: {agenda}
[当前讨论的 topic]: {current_topic}
[本轮要做的]: {role_hint}

要求：
- 用中文，简洁段落（不要 markdown 标题）
- 你的回复会被独立保存为一条群聊消息
- 严格只针对当前 topic 发言，不要跑题去其他 topic
- 不要重复别人已经说过的内容
- 没实质内容就直接说"无补充"+ @ 下一位
"""


def parse_topics_from_opening(text: str) -> list[str]:
    topics = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(?:T?\d+[\.:、]|[-*])\s*(.+)$", line.strip())
        if m:
            t = m.group(1).strip()
            if 3 <= len(t) <= 80 and "议程" not in t and "topic" not in t.lower():
                topics.append(t)
    return topics[:5]


def has_adjourn(text: str) -> bool:
    return any(kw in (text or "") for kw in ["散会", "会议结束", "adjourn"])


# ─── single topic loop ───────────────────────────────────────────────
def run_topic(date: str, idx: int, title: str, kind_hint: str,
              chair: str, members: list[str], session_for, agenda: str) -> bool:
    print(f"\n━━━ T{idx}: {title} ━━━")
    tid = store.start_topic(date, idx, title, kind=kind_hint)
    store.write_markdown(date)

    # 1. chair intro
    intro = call_agent(chair, session_for(chair), build_topic_prompt(
        date, chair, agenda, title,
        f"你是主席。我们刚开始讨论 [T{idx}: {title}]。请用 1-2 句话引入这个话题，"
        f"然后 @ 一个最相关的成员（{', '.join(m for m in members if m != chair)}）请他先发言。",
    ))
    if not is_empty(intro):
        store.add_post(date, tid, chair, intro)
        store.write_markdown(date)
        print(f"  [chair-intro] {chair}")
    if has_adjourn(intro):
        return True

    spoken: set[str] = set()
    queue: list[str] = store.extract_mentions(intro) or [
        m for m in members if m != chair
    ][:1]
    queue = [m for m in queue if m in members]
    turns = 0

    while queue and turns < PER_TOPIC_MAX_TURNS:
        speaker = queue.pop(0)
        if speaker == chair or speaker in spoken:
            continue
        turns += 1
        spoken.add(speaker)

        try:
            speech = call_agent(speaker, session_for(speaker), build_topic_prompt(
                date, speaker, agenda, title,
                f"你被点名在 [T{idx}: {title}] 这个 topic 下发言。"
                f"只针对这个 topic 简短表达（≤150 字）。可以 @ 其他成员追问或补充。"
                f"如果你没什么实质要说的，就回复一句话承接，并 @ 下一位。",
            ))
        except AgentError as e:
            print(f"  [error] {speaker}: {e}")
            store.add_post(date, tid, speaker, f"_(本轮调用失败：{e})_")
            store.write_markdown(date)
            continue

        if is_empty(speech):
            print(f"  [skip-empty] {speaker}")
            continue
        store.add_post(date, tid, speaker, speech)
        store.write_markdown(date)
        print(f"  [speak] {speaker}")

        for m in store.extract_mentions(speech):
            if m in members and m != chair and m not in spoken and m not in queue:
                queue.append(m)

    # 2. chair wrap
    if turns >= 1:
        try:
            wrap = call_agent(chair, session_for(chair), build_topic_prompt(
                date, chair, agenda, title,
                f"对 [T{idx}: {title}] 这个 topic 做一句话收束（行动项 / 决议）。"
                f"简短，不要 @ 任何人。",
            ))
            if not is_empty(wrap):
                store.add_post(date, tid, chair, wrap)
                store.write_markdown(date)
                print(f"  [wrap] {chair}")
        except AgentError as e:
            print(f"  [warn] chair wrap failed: {e}")

    return False


# ─── main flow ────────────────────────────────────────────────────────
def run_standup(date: str, members: list[str], chair: str) -> int:
    if chair not in members:
        members = [chair] + [m for m in members if m != chair]

    if store.is_locked_exclusive(date):
        print(f"⚠️  {date} 已经有一场早会在跑，跳过本次触发。")
        return 2

    def session_for(agent_id: str) -> str:
        return f"standup-{date}-{agent_id}"

    print(f"📝 events: {store.events_path(date)}")
    print(f"📄 markdown view: {store.md_path(date)}")
    print(f"🔒 每个 agent 独立 session：standup-{date}-<agent>")

    store.start_meeting(date, chair, members,
                        title=f"晨会纪要 · {date}")

    # snapshot each participant's main pointer BEFORE we issue any
    # `openclaw agent --session-id` (which clobbers main). We restore at end
    # via finally + atexit.
    snaps = snapshot_participants(date, members)
    if snaps:
        print(f"📌 已快照 {len(snaps)} 个 agent 的 main session 指针，会议结束自动恢复")

    # ── opening section ─────────────────────────────────────────────
    print("\n[phase 1] chair 开场 + 出议程")
    opening_topic_id = store.start_topic(date, 0, "开场 & 议程", kind="opening")
    store.write_markdown(date)

    open_prompt = f"""你是早会主席（{chair}）。今天参会：{', '.join(members)}。

请做两件事：
1. 用一句话开场（≤30 字）
2. 列出今天要讨论的 3 个 topic，格式严格如下：
   T1: <简短标题>
   T2: <简短标题>
   T3: <简短标题>

topic 选择建议：
- 第一个 topic 通常是"昨日进度同步"
- 第二个 topic 通常是"当前 blocker / 跨组依赖"
- 第三个 topic 是"今日重点 / 需要 Scott 决策的事项"

不要 @ 任何人，不要展开内容，只列纲。
"""
    try:
        opening = call_agent(chair, session_for(chair), open_prompt)
    except AgentError as e:
        print(f"❌ chair 开场失败：{e}")
        store.add_post(date, opening_topic_id, chair, f"_(开场失败：{e})_")
        store.finish_meeting(date)
        store.write_markdown(date)
        return 1

    store.add_post(date, opening_topic_id, chair, opening)
    store.write_markdown(date)

    topics = parse_topics_from_opening(opening)
    if not topics:
        print("⚠️ 未解析出 topics，使用默认 3 个")
        topics = ["昨日进度同步", "当前 blocker / 跨组依赖", "今日重点 / 待决策事项"]
    print(f"📋 议程（{len(topics)} 个 topic）")
    for i, t in enumerate(topics, 1):
        print(f"   T{i}: {t}")
    agenda = "; ".join(f"T{i}: {t}" for i, t in enumerate(topics, 1))

    # ── per-topic discussion ────────────────────────────────────────
    adjourned = False
    for idx, topic in enumerate(topics, 1):
        try:
            adjourned = run_topic(date, idx, topic, "topic",
                                  chair, members, session_for, agenda)
        except AgentError as e:
            print(f"❌ T{idx} 失败：{e}")
            continue
        if adjourned:
            print("⏹ chair 提前散会")
            break

    # ── closing summary ─────────────────────────────────────────────
    print("\n[phase 3] chair 收尾")
    closing_topic_id = store.start_topic(
        date, len(topics) + 1, "散会总结", kind="closing")
    store.write_markdown(date)

    final_prompt = f"""你是主席。整个会议讨论了：{agenda}

请做最终收尾：
1. **行动项**：按 owner 列出今日要推进的事（每人一行）
2. **Blocker**：明确列出阻塞项（没有就写"无"）
3. **🔴 需 Scott 决策**：列出需要老板拍板的事项（每条一行）
4. 最后一行写 `**散会**`

格式严格使用上面 4 个小标题。简短直接。
"""
    try:
        final = call_agent(chair, session_for(chair), final_prompt)
        store.add_post(date, closing_topic_id, chair, final)
    except AgentError as e:
        print(f"⚠️ 收尾失败：{e}")
        store.add_post(date, closing_topic_id, chair, f"_(收尾失败：{e})_")

    store.finish_meeting(date)
    store.write_markdown(date)

    n_restored = restore_participants()
    print(f"\n🔓 已恢复 {n_restored} 个 agent 的 main session 指针")
    print(f"✅ 会议结束")
    print(f"📄 完整纪要: {store.md_path(date)}")
    return 0


# ─── entry ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=datetime.date.today().isoformat())
    p.add_argument("--members", default=",".join(DEFAULT_MEMBERS))
    p.add_argument("--chair", default=DEFAULT_CHAIR)
    args = p.parse_args()

    if not store.is_valid_date(args.date):
        print(f"❌ invalid --date: {args.date!r}")
        sys.exit(2)

    members = [m.strip() for m in args.members.split(",") if m.strip()]
    if args.chair not in members:
        members.insert(0, args.chair)

    sys.exit(run_standup(args.date, members, args.chair))
