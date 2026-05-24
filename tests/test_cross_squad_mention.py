"""PR-5 — cross-squad mention support (PRD-v1.0 §2.2 Rule 5, §9).

Rule 5: scott may `@` an agent from another squad inside any thread.
The router is intentionally squad-agnostic: it routes purely on the
mention text, with no membership check against the thread's host squad.

These tests pin that contract down so a future "tighten membership"
refactor surfaces here and gets re-discussed (PRD §2.2 Rule 5).

Conclusion (Judy can verify): cross-squad mention is ALREADY supported
in the router. `post_router.py` contains zero references to `squad` or
`member`; `enqueue_if_needed` only requires `speaker == 'scott'` and
non-empty mentions. There is nothing to change in router for V1.0.0 \u2014
just lock the behavior with tests.
"""
from __future__ import annotations

import threading
import time

import pytest


# ─── helpers ─────────────────────────────────────────────────────────
def _two_squads(store):
    """Create two disjoint squads. 'alpha' has milk; 'beta' has cherry."""
    store.create_squad(
        {"id": "alpha", "name": "alpha", "members": ["milk", "scott"], "chair": "scott"}
    )
    store.create_squad(
        {"id": "beta", "name": "beta", "members": ["cherry", "scott"], "chair": "scott"}
    )


def _thread_in(store, squad_id: str, content: str):
    return store.create_thread(squad_id, "scott", content)


# ─── 1. baseline: same-squad mention dispatches ──────────────────────
def test_same_squad_mention_dispatches(router, store, monkeypatch):
    """Scott @milk inside alpha (milk's own squad) → routed."""
    _two_squads(store)
    calls: list[tuple] = []

    def fake_route(tid, ag, trig):
        calls.append((tid, ag))
    monkeypatch.setattr(router, "_route_to_agent_safely", fake_route)

    t = _thread_in(store, "alpha", "@milk please")
    post = {
        "speaker": "scott",
        "post_id": t["posts"][0]["id"],
        "mentions": ["milk"],
    }
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    time.sleep(0.2)
    assert ("milk" in [c[1] for c in calls])


# ─── 2. cross-squad: mention an agent NOT in the host squad ─────────
def test_cross_squad_mention_dispatches(router, store, monkeypatch):
    """Thread lives in alpha (members: milk, scott). Scott @cherry, who is
    only in beta. Router must still dispatch — squad boundaries are
    advisory, not enforced (PRD-v1.0 §2.2 Rule 5)."""
    _two_squads(store)
    calls: list[tuple] = []

    def fake_route(tid, ag, trig):
        calls.append((tid, ag))
    monkeypatch.setattr(router, "_route_to_agent_safely", fake_route)

    t = _thread_in(store, "alpha", "@cherry can you look at this?")
    post = {
        "speaker": "scott",
        "post_id": t["posts"][0]["id"],
        "mentions": ["cherry"],
    }
    dispatched = router.enqueue_if_needed(t["thread_id"], post)
    assert dispatched is True, "cross-squad mention should still route"
    time.sleep(0.2)
    assert "cherry" in [c[1] for c in calls], (
        "router dispatched the wrong agent — cross-squad expected `cherry`"
    )


# ─── 3. cross-squad: mix of same-squad and cross-squad in one post ──
def test_mixed_same_and_cross_squad_mentions(router, store, monkeypatch):
    """One scott post mentions both an in-squad and a cross-squad agent.
    Both should dispatch independently."""
    _two_squads(store)
    calls: list[tuple] = []

    def fake_route(tid, ag, trig):
        calls.append((tid, ag))
    monkeypatch.setattr(router, "_route_to_agent_safely", fake_route)

    t = _thread_in(store, "alpha", "@milk and @cherry, tag-team this please")
    post = {
        "speaker": "scott",
        "post_id": t["posts"][0]["id"],
        "mentions": ["milk", "cherry"],
    }
    assert router.enqueue_if_needed(t["thread_id"], post) is True
    time.sleep(0.3)
    agents_called = sorted({c[1] for c in calls})
    assert agents_called == ["cherry", "milk"], agents_called


# ─── 4. mention an id with no known agent — router still dispatches ─
def test_mention_unknown_agent_still_dispatches(router, store, monkeypatch):
    """Router does not pre-validate that the mentioned id exists as a real
    agent. The downstream `openclaw agent` invocation is what surfaces
    'unknown agent' as a router-error post. This is intentional: keeps the
    enqueue path cheap and the failure mode visible in the thread.

    What we DO want: router doesn't crash, and it still attempts to
    dispatch (so the user sees a clear error post if the agent id is bad)."""
    _two_squads(store)
    calls: list[tuple] = []

    def fake_route(tid, ag, trig):
        calls.append((tid, ag))
    monkeypatch.setattr(router, "_route_to_agent_safely", fake_route)

    t = _thread_in(store, "alpha", "@nobody_special hello?")
    post = {
        "speaker": "scott",
        "post_id": t["posts"][0]["id"],
        "mentions": ["nobody_special"],
    }
    dispatched = router.enqueue_if_needed(t["thread_id"], post)
    assert dispatched is True
    time.sleep(0.2)
    assert "nobody_special" in [c[1] for c in calls]


# ─── 5. router stays mention-permissive: no squad-membership filter ──
# V1.1: relaxed from "no `squad` word at all" to "no membership filtering".
# The strict text-ban was great as a tripwire but broke the moment we needed
# legitimate per-squad role lookups (e.g. @chair token → squad chair, see
# _resolve_chair_token in post_router.py). The PRD-v1.0 §2.2 Rule 5 contract
# is about not filtering OUT mentions based on squad membership, not about
# forbidding any squad read whatsoever. The patterns banned below are the
# actual membership-filter shapes that would re-narrow routing:
#   - `members`              (any membership-list iteration)
#   - `in squad`             ("if agent_id in squad..." guards)
#   - `not in squad`         (negative version of same)
#   - `if .*member`          (membership branching)
def test_router_has_no_squad_membership_filter():
    """Structural pin: post_router.py must not filter mentions by squad
    membership. If a future change adds a membership check, this test
    fails so PRD-v1.0 §2.2 Rule 5 gets re-discussed before merge."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "post_router.py"
    text = src.read_text(encoding="utf-8")
    # Strip Python comments and triple-quoted docstrings so the patterns
    # only inspect real code. Cheap-and-good-enough; if a future test fails
    # spuriously on docstring text, tighten this rather than the patterns.
    code = re.sub(r'""".*?"""', "", text, flags=re.S)
    code = re.sub(r"'''.*?'''", "", code, flags=re.S)
    code = re.sub(r"(?m)#.*$", "", code)
    banned = [
        r"\bmembers\b",
        r"\bin\s+squad",
        r"\bnot\s+in\s+squad",
        r"\bis_member\b",
    ]
    hits = [p for p in banned if re.search(p, code)]
    assert not hits, (
        f"post_router.py contains banned membership-filter pattern(s): {hits}. "
        "Cross-squad routing contract may have been narrowed. "
        "Re-read PRD-v1.0 §2.2 Rule 5 before merging."
    )
