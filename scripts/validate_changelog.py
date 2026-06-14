#!/usr/bin/env python3
"""Validate CHANGELOG.md has a user-facing section for the given tag.

Part of the OpenForge distribution SPEC §9.1 "changelog gate". Called by
`.github/workflows/release.yml` as a hard gate before building release
assets or publishing the npm CLI. Stable semver tags MUST pass; nightly
date tags skip the gate (workflow handles the if).

Exit code 0 = ok; non-zero = gate failure with reason on stderr.

Usage:
    python3 scripts/validate_changelog.py v1.2.0
"""

from __future__ import annotations

import pathlib
import re
import sys


def validate(tag: str, text: str) -> None:
    # 1. Section must exist as `## <tag>` (allow trailing date/note).
    section_re = re.compile(
        rf"^##\s+{re.escape(tag)}\b.*?(?=^##\s|\Z)", re.M | re.S
    )
    match = section_re.search(text)
    if not match:
        raise SystemExit(f"❌ CHANGELOG.md is missing '## {tag}' section")

    body = match.group(0).split("\n", 1)[1] if "\n" in match.group(0) else ""
    body = body.strip()

    # 2. Non-empty (>= 30 non-whitespace chars filters out placeholders).
    if len(re.sub(r"\s+", "", body)) < 30:
        raise SystemExit(
            f"❌ '## {tag}' section has <30 non-whitespace chars — looks like a placeholder"
        )

    # 3. No commit hashes (7-40 hex chars).
    hash_match = re.search(r"\b[0-9a-f]{7,40}\b", body)
    if hash_match:
        raise SystemExit(
            f"❌ '## {tag}' section contains commit hash {hash_match.group(0)!r}; "
            "rewrite in user-facing prose (see CHANGELOG.md writing rules comment at top)"
        )

    # 4. At least one bullet (- or *).
    if not re.search(r"^\s*[-*]\s+\S", body, re.M):
        raise SystemExit(
            f"❌ '## {tag}' section has no bullet list — use `- <user-facing change>` lines"
        )

    print(f"✅ CHANGELOG.md ok for {tag}")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: validate_changelog.py <tag>")
    tag = sys.argv[1]
    path = pathlib.Path("CHANGELOG.md")
    if not path.exists():
        raise SystemExit("❌ CHANGELOG.md not found at repo root")
    validate(tag, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
