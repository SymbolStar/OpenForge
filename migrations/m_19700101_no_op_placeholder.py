#!/usr/bin/env python3
"""migrations/m_19700101_no_op_placeholder.py — runner contract probe.

Idempotent no-op migration used to verify migrations.runner discovery
+ applied-tracking + JSON event emission end-to-end. ``applies_to`` is
set to ``v0.0.1`` so the runner only picks it up when somebody explicitly
asks for a range starting below that (e.g. ``--from v0.0.0 --to v0.0.1``).

Real migrations follow this same shape: META + ``up(data_dir)``.
"""
from __future__ import annotations

from pathlib import Path

META = {
    "id": "m_19700101_no_op_placeholder",
    "min_from": "v0.0.0",
    "applies_to": "v0.0.1",
}


def up(data_dir: Path) -> None:  # noqa: D401 - simple verb is fine
    """No-op. Touches nothing. Safe to call any number of times."""
    return
