"""Smoke tests for bin/openforge-worktree.

This is a shell script, but it's the one piece of agent-facing tooling
that PR-C2's preamble points every agent at. If it ever breaks the
multi-agent worktree workflow rule becomes an empty reference (which we
already lived through once — PR #1 was reverted because the workflow
wasn't actually runnable). So we do a tiny shell-out smoke test.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "bin" / "openforge-worktree"


def _run(*args: str, env_extra: dict[str, str] | None = None, check: bool = True,
         cwd: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Make sure no inherited override leaks in from the dev shell.
    env.pop("OPENFORGE_PROJECT_DIR", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(SCRIPT), *args],
        env=env, cwd=cwd,
        capture_output=True, text=True, check=check,
    )


def _make_repo(tmp_path: Path, name: str = "fake-repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    return repo


def test_script_is_executable():
    assert SCRIPT.exists(), SCRIPT
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


def test_help_does_not_require_repo():
    # --help must work even when there's no repo around (so agents seeing the
    # rule preamble can run --help safely from anywhere).
    r = _run("--help", check=True, cwd="/")
    assert "Usage:" in r.stdout
    assert "openforge-worktree add" in r.stdout


def test_add_stdout_is_only_the_worktree_path(tmp_path):
    repo = _make_repo(tmp_path)
    r = _run("add", "judy", "smoke",
             env_extra={"OPENFORGE_PROJECT_DIR": str(repo)})
    # Convention: stdout is exactly one absolute path; progress goes to stderr.
    out = r.stdout.strip()
    assert out.startswith("/"), out
    assert "\n" not in out
    assert Path(out).is_dir()
    expected = tmp_path / "fake-repo.worktrees" / "judy-smoke"
    assert Path(out).resolve() == expected.resolve()


def test_add_and_rm_round_trip(tmp_path):
    repo = _make_repo(tmp_path)
    env = {"OPENFORGE_PROJECT_DIR": str(repo)}
    r = _run("add", "alice", "task1", env_extra=env)
    wt = Path(r.stdout.strip())
    assert wt.is_dir()
    # ls should now show 2 entries: main repo + the new worktree.
    r = _run("ls", env_extra=env)
    assert "alice/task1" in r.stdout
    # rm should clean both the dir and the local branch.
    _run("rm", "alice/task1", env_extra=env)
    assert not wt.exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "alice/task1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branches == ""


def test_rm_refuses_unmerged_without_force(tmp_path):
    repo = _make_repo(tmp_path)
    env = {"OPENFORGE_PROJECT_DIR": str(repo)}
    wt = Path(_run("add", "judy", "unmerged", env_extra=env).stdout.strip())
    # Commit something on the worktree's branch so it's "ahead" of main.
    (wt / "newfile").write_text("x\n")
    subprocess.run(["git", "-C", str(wt), "add", "."], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "extra"], check=True)

    # Plain rm should fail (worktree has commits not on any merged base).
    r = _run("rm", "judy/unmerged", env_extra=env, check=False)
    assert r.returncode != 0
    # --force should succeed.
    r = _run("rm", "judy/unmerged", "--force", env_extra=env, check=True)
    assert "deleting local branch: judy/unmerged" in r.stderr


def test_add_rejects_existing_branch(tmp_path):
    repo = _make_repo(tmp_path)
    env = {"OPENFORGE_PROJECT_DIR": str(repo)}
    _run("add", "judy", "dup", env_extra=env)
    r = _run("add", "judy", "dup", env_extra=env, check=False)
    assert r.returncode != 0
    # Either guard fires first; both are fine, both block the dup.
    assert ("branch already exists" in r.stderr
            or "worktree path already exists" in r.stderr), r.stderr


def test_add_rejects_invalid_slugs(tmp_path):
    repo = _make_repo(tmp_path)
    env = {"OPENFORGE_PROJECT_DIR": str(repo)}
    r = _run("add", "judy", "has space", env_extra=env, check=False)
    assert r.returncode != 0
    assert "task-slug may only contain" in r.stderr


def test_no_repo_resolution_errors_loud(tmp_path):
    # Run from an empty dir with no env override and no .git.
    work = tmp_path / "nowhere"
    work.mkdir()
    r = _run("add", "judy", "x", cwd=str(work), check=False)
    assert r.returncode != 0
    assert "cannot find a git repo" in r.stderr


def test_repo_flag_overrides_env_and_cwd(tmp_path):
    repo_a = _make_repo(tmp_path, name="repo-a")
    repo_b = _make_repo(tmp_path, name="repo-b")
    # OPENFORGE_PROJECT_DIR points at A, but --repo says B → B wins.
    r = _run("--repo", str(repo_b), "add", "alice", "explicit",
             env_extra={"OPENFORGE_PROJECT_DIR": str(repo_a)})
    out = Path(r.stdout.strip())
    assert "repo-b.worktrees" in str(out)
    # Cleanup so repeated runs don't leak worktree state.
    shutil.rmtree(out, ignore_errors=True)
