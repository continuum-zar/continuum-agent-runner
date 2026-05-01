"""run_shell tool: allow-listed shell command execution inside the workspace."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any

from runner.config import settings
from runner.sandbox.shell import ShellPolicyError, run_shell_capped

if TYPE_CHECKING:
    from runner.agent.context import RunContext


async def handle(ctx: "RunContext", args: dict[str, Any]) -> str:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return "[runner] run_shell: 'command' is required"

    timeout = args.get("timeout_seconds")
    try:
        timeout_int = int(timeout) if timeout is not None else 120
    except (TypeError, ValueError):
        timeout_int = 120
    timeout_int = max(5, min(timeout_int, settings.MAX_SHELL_TIMEOUT_SECONDS))

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return f"[runner] could not parse command: {exc}"

    try:
        rc, stdout, stderr = await run_shell_capped(
            argv,
            cwd=ctx.workspace.root,
            timeout_seconds=timeout_int,
        )
    except ShellPolicyError as exc:
        # Surface a helpful, structured rejection so the model corrects course.
        return (
            f"[runner] command rejected by sandbox policy: {exc}\n"
            "Allowed: typical dev tools (git, npm, pnpm, yarn, node, python, "
            "pip, pytest, ruff, mypy, go, cargo, make, ls, cat, etc.). Network "
            "egress and destructive admin commands are blocked."
        )

    # Stream a compact stdout chunk to the live event hub so the UI gets to
    # show what the command produced without waiting for the model's next turn.
    if stdout:
        await ctx.events.emit("shell_stdout", {"chunk": stdout[-2000:]})

    body = (
        f"$ {' '.join(shlex.quote(a) for a in argv)}\n"
        f"exit_code={rc}\n"
        f"--- stdout ---\n{stdout or '(empty)'}\n"
        f"--- stderr ---\n{stderr or '(empty)'}"
    )
    return body
