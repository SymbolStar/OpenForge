"""Minimal ACP CLI bridge for OpenForge.

This is the PR-2 MVP: route selected ACP employees directly through their
local CLI in oneshot mode. A fuller protocol abstraction belongs in a later
PR; this module keeps the current behavior explicit and easy to replace.
"""
from __future__ import annotations

import os
import subprocess

from agent_runtime import AgentError, clean

ACP_AGENT_TIMEOUT = int(os.environ.get("OPENFORGE_ACP_AGENT_TIMEOUT", "240"))


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
    try:
        proc = subprocess.run(
            argv,
            cwd=run_cwd,
            env=spawn_env,
            capture_output=True,
            text=True,
            timeout=ACP_AGENT_TIMEOUT,
        )
    except FileNotFoundError:
        raise AgentError(f"ACP CLI not found for {agent_id}: {argv[0]}") from None
    except subprocess.TimeoutExpired:
        raise AgentError(f"ACP {agent_id} timeout after {ACP_AGENT_TIMEOUT}s") from None

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise AgentError(
            f"ACP {agent_id} exited {proc.returncode}: " + " | ".join(tail)
        )
    out = clean(proc.stdout or "")
    if not out:
        raise AgentError(f"ACP {agent_id} produced no output")
    return out
