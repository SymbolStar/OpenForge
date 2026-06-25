"""Minimal ACP CLI bridge for OpenForge.

This is the PR-2 MVP: route selected ACP employees directly through their
local CLI in oneshot mode. A fuller protocol abstraction belongs in a later
PR; this module keeps the current behavior explicit and easy to replace.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading

from agent_runtime import AgentError, clean, _killpg_safe

ACP_AGENT_TIMEOUT = int(os.environ.get("OPENFORGE_ACP_AGENT_TIMEOUT", "240"))

# chip_post_id → pgid for in-flight ACP CLI invocations. Used by the
# cancel endpoint to SIGTERM the subprocess group on ✖ click.
_acp_chip_to_pgid: dict[str, int] = {}
_acp_chip_to_pgid_lock = threading.Lock()


def cancel_acp_chip_subprocess(chip_post_id: str) -> bool:
    with _acp_chip_to_pgid_lock:
        pgid = _acp_chip_to_pgid.get(chip_post_id)
    if pgid is None:
        return False
    _killpg_safe(pgid, signal.SIGTERM)
    return True


def _argv_for_agent(agent_id: str, prompt: str) -> list[str]:
    if agent_id == "codex":
        return [
            "codex", "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            prompt,
        ]
    if agent_id == "claude":
        return ["claude", "-p", prompt]
    raise AgentError(f"unsupported ACP employee: {agent_id}")


def call_acp_agent(
    agent_id: str,
    session_id: str,
    prompt: str,
    extra_env: dict[str, str] | None = None,
    cwd: str | None = None,
    chip_post_id: str | None = None,
) -> str:
    """Invoke an ACP-backed CLI employee in oneshot mode.

    ``session_id`` is accepted for API parity with ``agent_runtime.call_agent``;
    the MVP CLI path is stateless and does not use it yet.
    """
    del session_id
    run_cwd = cwd or (extra_env or {}).get("OPENFORGE_PROJECT_DIR") or None
    spawn_env = None
    if extra_env:
        cleaned = {k: v for k, v in extra_env.items() if v is not None}
        if cleaned:
            spawn_env = {**os.environ, **cleaned}
    argv = _argv_for_agent(agent_id, prompt)
    proc: subprocess.Popen | None = None
    pgid: int | None = None
    try:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=run_cwd,
                env=spawn_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise AgentError(f"ACP CLI not found for {agent_id}: {argv[0]}") from None
        pgid = proc.pid
        if chip_post_id:
            with _acp_chip_to_pgid_lock:
                _acp_chip_to_pgid[chip_post_id] = pgid
        try:
            stdout, stderr = proc.communicate(timeout=ACP_AGENT_TIMEOUT)
        except subprocess.TimeoutExpired:
            _killpg_safe(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _killpg_safe(pgid, signal.SIGKILL)
            raise AgentError(f"ACP {agent_id} timeout after {ACP_AGENT_TIMEOUT}s") from None
        returncode = proc.returncode
        if returncode != 0:
            tail = (stderr or stdout or "").strip().splitlines()[-3:]
            raise AgentError(
                f"ACP {agent_id} exited {returncode}: " + " | ".join(tail)
            )
        out = clean(stdout or "")
        if not out:
            raise AgentError(f"ACP {agent_id} produced no output")
        return out
    finally:
        if chip_post_id:
            with _acp_chip_to_pgid_lock:
                _acp_chip_to_pgid.pop(chip_post_id, None)
