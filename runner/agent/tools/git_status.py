"""git_status tool."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from runner.sandbox.shell import run_shell_capped

if TYPE_CHECKING:
    from runner.agent.context import RunContext


async def handle(ctx: "RunContext", args: dict[str, Any]) -> str:  # noqa: ARG001
    rc, stdout, stderr = await run_shell_capped(
        ["git", "status", "--porcelain", "--branch"],
        cwd=ctx.workspace.root,
        timeout_seconds=20,
    )
    if rc != 0:
        return f"[runner] git status failed: {stderr}"
    return stdout or "(clean working tree)"
