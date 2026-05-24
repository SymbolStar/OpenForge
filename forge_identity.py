"""forge_identity.py — agent display-name layer for OpenForge.

PRD-v1.2 (Scott 2026-05-24 21:22): forge UI should show the friendly
**Name** from each agent's `~/.openclaw/workspace-<id>/IDENTITY.md`
instead of the raw agent id. The router maps `@<display_name>` back to
the agent id when dispatching, so users can write the way they speak
('@Dora') and still hit the right subprocess (`designer`).

This module is the single source of truth for that mapping.

Storage stays canonical:
  * `post.speaker` is always the agent id ('designer').
  * `mentions` in events stores whatever the regex captured ('Dora' OR
    'designer' — both legal).
  * Display & routing layers ask this module to translate either way.

IDENTITY.md format (the first matched line wins):
    - **Name:** Dora
    - **Emoji:** 🎨

Compound names like '小巴 (Xiaoba / Buffett)' are accepted as-is for
display; for *resolution* we additionally split on ' (' / ',' / '/' so
all of @小巴, @Xiaoba, @Buffett resolve to xiaoba.

The mapping is rebuilt fresh on every call (no cache). The set of
employees rarely changes and we'd rather take a few-ms hit per request
than serve a stale name after the user edits IDENTITY.md.
"""
from __future__ import annotations

import re
from pathlib import Path

import forge_employees

# Match the same '- **Name:** X' / '- **Emoji:** X' lines used in our
# IDENTITY.md template. Tolerant of surrounding whitespace; case-
# sensitive on the key to avoid matching narrative prose like
# '...he goes by the name dora here...'.
_NAME_RE = re.compile(r"^\s*-\s*\*\*Name:\*\*\s*(.+?)\s*$", re.MULTILINE)
_EMOJI_RE = re.compile(r"^\s*-\s*\*\*Emoji:\*\*\s*(.+?)\s*$", re.MULTILINE)


def _identity_path(agent_id: str) -> Path:
    return Path.home() / ".openclaw" / f"workspace-{agent_id}" / "IDENTITY.md"


def _looks_like_placeholder(value: str) -> bool:
    """True if the IDENTITY.md value is a literal template placeholder
    instead of a real name/emoji. The shipped template uses italics
    like '_(pick something you like)_' and '_(your signature — pick one
    that feels right)_'."""
    v = (value or "").strip()
    if not v:
        return True
    # Markdown italic placeholders the template ships with
    if v.startswith("_(") and v.endswith(")_"):
        return True
    # Defensive: any value wrapped in plain parens with 'pick' in it
    if v.startswith("(") and v.endswith(")") and "pick" in v.lower():
        return True
    return False


def _parse_identity(text: str) -> tuple[str, str]:
    """Return (display_name, emoji). Either may be '' when missing OR
    when the IDENTITY.md still has the template placeholder text."""
    name_m = _NAME_RE.search(text or "")
    emoji_m = _EMOJI_RE.search(text or "")
    raw_name = name_m.group(1).strip() if name_m else ""
    raw_emoji = emoji_m.group(1).strip() if emoji_m else ""
    name = "" if _looks_like_placeholder(raw_name) else raw_name
    emoji = "" if _looks_like_placeholder(raw_emoji) else raw_emoji
    return name, emoji


def get_identity(agent_id: str) -> dict:
    """Return {id, name, emoji} for `agent_id`. Falls back to the agent
    id itself for name when IDENTITY.md is missing / malformed, so
    callers never have to special-case unknown employees."""
    if not agent_id:
        return {"id": "", "name": "", "emoji": ""}
    path = _identity_path(agent_id)
    name, emoji = ("", "")
    if path.exists():
        try:
            name, emoji = _parse_identity(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "id": agent_id,
        "name": name or agent_id,
        "emoji": emoji,
    }


def list_identities() -> list[dict]:
    """Return [{id, name, emoji}, ...] for every real employee.
    Sorted by id for stability (matches /api/employees)."""
    return [get_identity(aid) for aid in forge_employees.list_employees()]


def _alias_tokens(display_name: str) -> list[str]:
    """Break a compound display name into individual lookup tokens.

    '小巴 (Xiaoba / Buffett)' → ['小巴', 'Xiaoba', 'Buffett']
    'Dora'                    → ['Dora']
    'Alice (PM)'              → ['Alice', 'PM']    ← acceptable; the PM
       alias is unique-ish and dropping it would over-engineer this.

    Caller is expected to dedupe & filter empty before use.
    """
    if not display_name:
        return []
    # Strip any parenthesised aliases first so the head token is clean,
    # then walk the inside on common separators (/, ,, ;, &).
    out: list[str] = []
    # Split on whitespace+( so 'Dora (Designer)' splits cleanly.
    head, _, tail = display_name.partition("(")
    for tok in re.split(r"[\s,/&;]+", head):
        t = tok.strip()
        if t:
            out.append(t)
    if tail:
        inner = tail.rstrip(") ").strip()
        for tok in re.split(r"[,/&;]+", inner):
            t = tok.strip()
            if t:
                out.append(t)
    return out


def name_to_id(name: str) -> str | None:
    """Resolve an @-mention spelling to a canonical agent id.

    Lookup order:
      1. exact agent_id match (case-insensitive)            → 'designer' → 'designer'
      2. exact display-name match (case-insensitive)        → 'Dora'     → 'designer'
      3. alias token from a compound display name           → 'Xiaoba'   → 'xiaoba'
                                                              → '小巴'    → 'xiaoba'

    Returns None if no employee matches. The router uses None as 'leave
    the mention alone' so non-employee mentions (router IDs, free-form
    @scott, typos) still flow through the existing reserved-set
    filtering downstream.
    """
    if not name:
        return None
    key = name.strip().lower()
    if not key:
        return None
    ids = forge_employees.list_employees()
    id_set = {aid.lower(): aid for aid in ids}
    # 1. agent_id direct hit
    if key in id_set:
        return id_set[key]
    # 2 & 3. walk identities for display-name & alias matches
    for aid in ids:
        ident = get_identity(aid)
        candidates = _alias_tokens(ident.get("name") or "")
        for cand in candidates:
            if cand.lower() == key:
                return aid
    return None
