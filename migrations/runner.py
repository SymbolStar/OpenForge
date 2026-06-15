#!/usr/bin/env python3
"""migrations/runner.py — sequence migrations between two OpenForge versions.

Called by the npm @symbolstar/openforge updater after switching the
``current`` symlink to a new version (SPEC §6). Idempotent and
re-entrant: a migration that has already been applied is skipped on the
next run.

Each migration script in this package that wants to be sequenced exposes
a ``META`` dict and an ``up(data_dir: Path) -> None`` callable:

.. code-block:: python

    META = {
        "id": "m_YYYYMMDD_short_description",
        "min_from": "v1.1.0",   # earliest source version this applies from
        "applies_to": "v1.2.0", # version this is shipped with
    }

    def up(data_dir):
        ...

Runner contract (stable; the CLI talks to this):

* CLI sets stdin/stdout text mode; we honor that.
* stdout = human-readable log; stderr = one JSON line per emitted event
  ({"event": "applied"|"skipped"|"error", "id": ..., "elapsed_ms": ...}).
* Exit codes::

      0 = success or nothing to do
      2 = user-fixable failure (schema clash etc.); error message on stderr
      other = unexpected system error (traceback on stderr)

* Applied IDs are persisted to ``<data_dir>/.migrations-applied.json``.
* Legacy free-standing scripts (those without ``META``) are ignored by
  the runner; they remain runnable by hand for back-compat.

Usage::

    python3 -m migrations.runner --from v1.1.0 --to v1.2.0 \\
        --data-dir ~/.openforge
"""
from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# Re-export package for `python3 -m migrations.runner`.
import migrations  # noqa: F401  (package init side-effects)

APPLIED_FILE = ".migrations-applied.json"


def _emit(event: str, **kwargs: Any) -> None:
    """One-line JSON event on stderr for CLI consumption."""
    payload = {"event": event, **kwargs}
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def _parse_semver(v: str) -> tuple[int, int, int] | None:
    """Parse ``vMAJOR.MINOR.PATCH``. Returns None for non-semver (e.g. nightly date tags).

    Nightly tags (vYYYY.MM.DD) won't parse here, which is fine — they're
    not gated, but they're also not used as range boundaries in stable.
    For ordering we treat anything non-semver as "outside the range".
    """
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", v.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _in_range(applies_to: str, from_v: str, to_v: str) -> bool:
    """Is ``applies_to`` strictly above ``from_v`` and at most ``to_v``?"""
    a = _parse_semver(applies_to)
    f = _parse_semver(from_v)
    t = _parse_semver(to_v)
    if a is None or f is None or t is None:
        # Be conservative: if any side isn't semver, skip ordered selection.
        # Caller can still apply manually.
        return False
    return f < a <= t


def _discover() -> list[Any]:
    """Return migration modules that expose META + up(). Sorted by id."""
    mods = []
    for info in pkgutil.iter_modules(migrations.__path__):
        if info.name == "runner":
            continue
        mod = importlib.import_module(f"migrations.{info.name}")
        meta = getattr(mod, "META", None)
        up = getattr(mod, "up", None)
        if isinstance(meta, dict) and callable(up):
            mods.append(mod)
    mods.sort(key=lambda m: m.META["id"])
    return mods


def _load_applied(data_dir: Path) -> set[str]:
    p = data_dir / APPLIED_FILE
    if not p.exists():
        return set()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        return set(doc.get("applied", []))
    except Exception:
        # Corrupt file: don't lose data, just treat as empty and let the
        # next successful write rebuild it.
        return set()


def _save_applied(data_dir: Path, applied: set[str]) -> None:
    p = data_dir / APPLIED_FILE
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"applied": sorted(applied)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)


def run(from_v: str, to_v: str, data_dir: Path) -> int:
    data_dir = data_dir.expanduser().resolve()
    if not data_dir.exists():
        print(f"data dir does not exist: {data_dir}", file=sys.stderr)
        return 2
    applied = _load_applied(data_dir)
    mods = _discover()

    pending = [
        m for m in mods
        if _in_range(m.META.get("applies_to", ""), from_v, to_v)
        and m.META["id"] not in applied
    ]
    if not pending:
        print(f"no migrations to apply ({from_v} → {to_v})")
        return 0

    print(f"applying {len(pending)} migration(s) ({from_v} → {to_v})")
    for mod in pending:
        mid = mod.META["id"]
        t0 = time.monotonic()
        try:
            mod.up(data_dir)
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            traceback.print_exc()
            _emit("error", id=mid, elapsed_ms=elapsed_ms, message=str(exc))
            return 2
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        applied.add(mid)
        _save_applied(data_dir, applied)
        print(f"  ✓ {mid} ({elapsed_ms} ms)")
        _emit("applied", id=mid, elapsed_ms=elapsed_ms)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrations.runner",
        description="Apply OpenForge data migrations between two versions.",
    )
    parser.add_argument("--from", dest="from_v", required=True, help="Old version tag, e.g. v1.1.0")
    parser.add_argument("--to", dest="to_v", required=True, help="New version tag, e.g. v1.2.0")
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="OpenForge data root (typically ~/.openforge)",
    )
    args = parser.parse_args(argv)
    return run(args.from_v, args.to_v, args.data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
