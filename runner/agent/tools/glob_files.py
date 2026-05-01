"""glob_files tool: fast tracked-file lookup via git pathspecs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from runner.sandbox.shell import run_shell_capped

if TYPE_CHECKING:
    from runner.agent.context import RunContext


async def handle(ctx: "RunContext", args: dict[str, Any]) -> str:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return "[runner] glob_files: 'pattern' is required"

    try:
        max_results = int(args.get("max_results", 200) or 200)
    except (TypeError, ValueError):
        max_results = 200
    max_results = max(1, min(max_results, 1000))

    pathspec = pattern if pattern.startswith(":(") else f":(glob){pattern}"
    rc, stdout, stderr = await run_shell_capped(
        ["git", "ls-files", "--", pathspec],
        cwd=ctx.workspace.root,
        timeout_seconds=30,
    )
    if rc != 0:
        return f"[runner] glob_files failed: {stderr or stdout}"

    files = [line for line in stdout.splitlines() if line]
    shown = files[:max_results]
    out = "\n".join(shown) if shown else "[runner] no files matched"
    if len(files) > max_results:
        out += f"\n[runner] matches truncated at {max_results} of {len(files)} files"
    return out
