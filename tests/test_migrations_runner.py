"""Tests for migrations.runner (SPEC §6 contract)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from migrations import runner


REPO = Path(__file__).resolve().parent.parent


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "migrations.runner", *args],
        cwd=cwd or REPO,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "openforge"
    d.mkdir()
    return d


def test_in_range_helper():
    assert runner._in_range("v1.2.0", "v1.1.0", "v1.2.0") is True
    assert runner._in_range("v1.1.0", "v1.1.0", "v1.2.0") is False  # strict >
    assert runner._in_range("v1.3.0", "v1.1.0", "v1.2.0") is False  # > to
    assert runner._in_range("v2026.06.04", "v1.1.0", "v1.2.0") is False  # nightly tag


def test_nothing_to_apply_when_out_of_range(data_dir: Path):
    rc = runner.run("v1.1.0", "v1.2.0", data_dir)
    # placeholder migration applies_to v0.0.1, well below the range
    assert rc == 0
    assert not (data_dir / runner.APPLIED_FILE).exists()


def test_applies_then_skips_on_rerun(data_dir: Path):
    rc = runner.run("v0.0.0", "v0.0.1", data_dir)
    assert rc == 0
    applied = json.loads((data_dir / runner.APPLIED_FILE).read_text())
    assert "m_19700101_no_op_placeholder" in applied["applied"]

    # second run: same range, should be a no-op (idempotent)
    rc = runner.run("v0.0.0", "v0.0.1", data_dir)
    assert rc == 0
    applied2 = json.loads((data_dir / runner.APPLIED_FILE).read_text())
    assert applied2 == applied


def test_missing_data_dir_returns_2(tmp_path: Path):
    rc = runner.run("v0.0.0", "v0.0.1", tmp_path / "does-not-exist")
    assert rc == 2


def test_cli_smoke(data_dir: Path):
    proc = _run_cli("--from", "v0.0.0", "--to", "v0.0.1", "--data-dir", str(data_dir))
    assert proc.returncode == 0, proc.stderr
    # stdout has the human log
    assert "applying 1 migration" in proc.stdout
    # stderr has one structured event line
    events = [json.loads(line) for line in proc.stderr.strip().splitlines() if line.strip()]
    assert any(e["event"] == "applied" and e["id"] == "m_19700101_no_op_placeholder" for e in events)


def test_failing_migration_returns_2(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    # Inject a fake module into the discovery result that raises in up().
    class FakeMod:
        META = {"id": "m_99999999_fails", "min_from": "v0.0.0", "applies_to": "v0.0.1"}

        @staticmethod
        def up(dd: Path) -> None:
            raise RuntimeError("schema clash simulated")

    monkeypatch.setattr(runner, "_discover", lambda: [FakeMod])
    rc = runner.run("v0.0.0", "v0.0.1", data_dir)
    assert rc == 2
    # Applied file should not contain the failed migration id
    if (data_dir / runner.APPLIED_FILE).exists():
        applied = json.loads((data_dir / runner.APPLIED_FILE).read_text())
        assert "m_99999999_fails" not in applied.get("applied", [])
