"""list_dir tool: list a directory inside the workspace."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from runner.sandbox.workspace import WorkspacePathError

if TYPE_CHECKING:
    from runner.agent.context import RunContext


async def handle(ctx: "RunContext", args: dict[str, Any]) -> str:
    path = args.get("path", ".")
    max_entries = int(args.get("max_entries", 200) or 200)
    if max_entries <= 0 or max_entries > 2000:
        max_entries = 200
    try:
        target = ctx.workspace.resolve(path)
    except WorkspacePathError as exc:
        return f"[runner] {exc}"
    if not target.exists():
        return f"[runner] path does not exist: {path!r}"
    if not target.is_dir():
        return f"[runner] path is not a directory: {path!r}"

    entries: list[str] = []
    truncated = False
    for i, entry in enumerate(sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))):
        if i >= max_entries:
            truncated = True
            break
        if entry.name in (".git",):
            entries.append(f"d  {entry.name}/  (skipped)")
            continue
        if entry.is_dir():
            entries.append(f"d  {entry.name}/")
        else:
            try:
                size = entry.stat().st_size
                entries.append(f"f  {entry.name}  ({size} bytes)")
            except OSError:
                entries.append(f"f  {entry.name}")

    out = "\n".join(entries) if entries else "(empty)"
    if truncated:
        out += f"\n[runner] listing truncated at {max_entries} entries"
    return out
