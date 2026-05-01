"""grep_files tool: fast repo text search via ripgrep."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from runner.sandbox.shell import run_shell_capped
from runner.sandbox.workspace import WorkspacePathError

if TYPE_CHECKING:
    from runner.agent.context import RunContext


async def handle(ctx: "RunContext", args: dict[str, Any]) -> str:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return "[runner] grep_files: 'pattern' is required"

    path = args.get("path", ".")
    if not isinstance(path, str) or not path:
        return "[runner] grep_files: 'path' must be a non-empty string"

    try:
        target = ctx.workspace.resolve(path)
    except WorkspacePathError as exc:
        return f"[runner] {exc}"
    if not target.exists():
        return f"[runner] path does not exist: {path!r}"

    try:
        max_matches = int(args.get("max_matches", 100) or 100)
    except (TypeError, ValueError):
        max_matches = 100
    max_matches = max(1, min(max_matches, 500))

    argv = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color",
        "never",
        "--max-columns",
        "240",
        "--max-columns-preview",
    ]

    glob = args.get("glob")
    if isinstance(glob, str) and glob:
        argv.extend(["--glob", glob])

    argv.extend([pattern, ctx.workspace.relative(target)])

    rc, stdout, stderr = await run_shell_capped(
        argv,
        cwd=ctx.workspace.root,
        timeout_seconds=30,
    )
    if rc == 1:
        return "[runner] no matches"
    if rc != 0:
        return f"[runner] grep_files failed: {stderr or stdout}"

    lines = stdout.splitlines()
    shown = lines[:max_matches]
    out = "\n".join(shown) if shown else "[runner] no matches"
    if len(lines) > max_matches:
        out += f"\n[runner] matches truncated at {max_matches} of {len(lines)} visible matches"
    return out
