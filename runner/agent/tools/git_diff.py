"""git_diff tool."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from runner.sandbox.shell import run_shell_capped
from runner.sandbox.workspace import WorkspacePathError

if TYPE_CHECKING:
    from runner.agent.context import RunContext


async def handle(ctx: "RunContext", args: dict[str, Any]) -> str:
    staged = bool(args.get("staged", False))
    path = args.get("path")

    argv = ["git", "diff"]
    if staged:
        argv.append("--cached")
    argv.append("--no-color")

    if path:
        if not isinstance(path, str):
            return "[runner] git_diff: 'path' must be a string"
        try:
            target = ctx.workspace.resolve(path)
        except WorkspacePathError as exc:
            return f"[runner] {exc}"
        argv.extend(["--", ctx.workspace.relative(target)])

    rc, stdout, stderr = await run_shell_capped(
        argv,
        cwd=ctx.workspace.root,
        timeout_seconds=30,
    )
    if rc != 0:
        return f"[runner] git diff failed: {stderr}"
    return stdout or "(no changes)"
