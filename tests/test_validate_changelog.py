"""Tests for scripts/validate_changelog.py (release gate)."""

from __future__ import annotations

# Pull the function under test from the script. The script is not a package,
# so we exec it once into a namespace for direct access.
import pathlib
import runpy

import pytest

_NS = runpy.run_path(
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "validate_changelog.py"),
    run_name="<test>",
)
validate = _NS["validate"]


GOOD = """\
# Changelog

## v1.2.0

- Squad rename in the sidebar
- Fix thread auto-scroll flicker when many posts land at once
- events.jsonl now appends incrementally
"""


def test_good_passes(capsys):
    validate("v1.2.0", GOOD)
    out = capsys.readouterr().out
    assert "ok for v1.2.0" in out


def test_missing_section():
    with pytest.raises(SystemExit, match="missing '## v9.9.9' section"):
        validate("v9.9.9", GOOD)


def test_empty_section():
    bad = "# Changelog\n\n## v1.2.0\n\nWIP\n"
    with pytest.raises(SystemExit, match="<30 non-whitespace chars"):
        validate("v1.2.0", bad)


def test_commit_hash_blocked():
    bad = "# Changelog\n\n## v1.2.0\n\n- Fix the bug introduced in abc1234 that broke threads\n"
    with pytest.raises(SystemExit, match="contains commit hash"):
        validate("v1.2.0", bad)


def test_no_bullet():
    bad = (
        "# Changelog\n\n## v1.2.0\n\nThis release brings many improvements "
        "and bug fixes to the user experience.\n"
    )
    with pytest.raises(SystemExit, match="no bullet list"):
        validate("v1.2.0", bad)


def test_trailing_section_only():
    # tag matches first section, body bounded by next `## ` — bullets in
    # the *next* section must not satisfy the gate for this tag.
    text = (
        "# Changelog\n\n## v2.0.0\n\nWIP\n\n## v1.0.0\n\n- Something user-facing here works\n"
    )
    with pytest.raises(SystemExit, match="<30 non-whitespace chars"):
        validate("v2.0.0", text)
