#!/usr/bin/env python3
"""
migrate_md_to_jsonl.py — one-shot importer for legacy standup-*.md files.

Walks ~/.openclaw/standups/standup-YYYY-MM-DD.md, parses sections + posts,
and emits an equivalent events.jsonl into the new data/<date>/ tree.

Idempotent: skips dates that already have an events.jsonl.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import huddle_store as store

RE_TITLE = re.compile(r"^#\s+晨会纪要\s+·\s+(.+)$", re.MULTILINE)
RE_META = re.compile(r"\*\*主席\*\*:\s*(\S+)\s+·\s+\*\*参会\*\*:\s*(.+)")
RE_SECTION = re.compile(r"^##\s+(.+)$", re.MULTILINE)
RE_POST = re.compile(r"^####\s+(\S+)\s+·\s+(\S+)\s*$", re.MULTILINE)
# v0.1 style
RE_POST_OLD = re.compile(r"^###\s+(\S+)\s+·\s+(\S+)\s*$", re.MULTILINE)


def classify(title: str) -> str:
    if title.startswith("T") and "·" in title:
        return "topic"
    if "议程" in title or "开场" in title:
        return "opening"
    if "散会" in title or "总结" in title:
        return "closing"
    return "other"


def parse_md(text: str):
    title_m = RE_TITLE.search(text)
    meta = RE_META.search(text)
    chair = meta.group(1) if meta else "?"
    members = [s.strip() for s in meta.group(2).split(",")] if meta else []

    # split by ## sections; if there are none, treat whole body as one section
    section_indices = [(m.start(), m.group(1).strip())
                       for m in RE_SECTION.finditer(text)]
    sections = []
    if section_indices:
        for i, (start, title) in enumerate(section_indices):
            end = section_indices[i + 1][0] if i + 1 < len(section_indices) else len(text)
            body = text[start:end]
            body = re.sub(r"^##\s+.+\n", "", body, count=1)
            sections.append((title, body))
    else:
        sections.append(("正文", text))

    out_sections = []
    for title, body in sections:
        # try ####, fall back to ###
        matches = list(RE_POST.finditer(body)) or list(RE_POST_OLD.finditer(body))
        posts = []
        for i, m in enumerate(matches):
            speaker = m.group(1)
            time = m.group(2)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            content = body[start:end].strip()
            posts.append((speaker, time, content))
        out_sections.append((title, classify(title), posts))

    return {
        "title": title_m.group(1) if title_m else "",
        "chair": chair,
        "members": members,
        "sections": out_sections,
    }


def migrate_one(md_path: Path, force: bool = False) -> bool:
    m = re.match(r"standup-(\d{4}-\d{2}-\d{2})\.md", md_path.name)
    if not m:
        return False
    date = m.group(1)
    events_path = store.events_path(date)
    if events_path.exists() and not force:
        print(f"⏭  {date}: events.jsonl 已存在，跳过")
        return False

    parsed = parse_md(md_path.read_text(encoding="utf-8"))
    print(f"📥 {date}: chair={parsed['chair']} members={parsed['members']} "
          f"sections={len(parsed['sections'])}")

    if force and events_path.exists():
        events_path.unlink()

    store.start_meeting(date, parsed["chair"], parsed["members"],
                        title=parsed["title"] or date)
    for idx, (title, kind, posts) in enumerate(parsed["sections"], 1):
        tid = store.start_topic(date, idx, title, kind=kind)
        for speaker, _time, content in posts:
            if not content.strip():
                continue
            store.add_post(date, tid, speaker, content)
    store.finish_meeting(date)
    store.write_markdown(date)
    return True


def main():
    force = "--force" in sys.argv
    if force:
        print("⚠️  --force: 会重建已存在的 events.jsonl")
    candidates = sorted(store.STANDUP_DIR.glob("standup-*.md"))
    if not candidates:
        print("没找到任何 standup-*.md")
        return
    n = 0
    for p in candidates:
        if migrate_one(p, force=force):
            n += 1
    print(f"\n✅ 完成。新导入 {n} 个会议。")


if __name__ == "__main__":
    main()
