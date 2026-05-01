"""read_file tool."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from runner.config import settings
from runner.sandbox.workspace import WorkspacePathError

if TYPE_CHECKING:
    from runner.agent.context import RunContext


async def handle(ctx: "RunContext", args: dict[str, Any]) -> str:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return "[runner] read_file: 'path' is required"
    start_line = args.get("start_line")
    end_line = args.get("end_line")

    try:
        target = ctx.workspace.resolve(path)
    except WorkspacePathError as exc:
        return f"[runner] {exc}"
    if not target.exists():
        return f"[runner] file does not exist: {path!r}"
    if not target.is_file():
        return f"[runner] not a regular file: {path!r}"

    cap = settings.MAX_FILE_BYTES
    try:
        size = target.stat().st_size
    except OSError as exc:
        return f"[runner] could not stat file: {exc}"

    truncated = False
    if size > cap:
        with target.open("rb") as fh:
            data = fh.read(cap)
        truncated = True
    else:
        with target.open("rb") as fh:
            data = fh.read()

    text = data.decode("utf-8", errors="replace")

    if isinstance(start_line, int) or isinstance(end_line, int):
        lines = text.splitlines()
        s = max(1, int(start_line)) if isinstance(start_line, int) else 1
        e = min(len(lines), int(end_line)) if isinstance(end_line, int) else len(lines)
        if s > e:
            return "[runner] start_line is greater than end_line"
        slice_ = lines[s - 1 : e]
        text = "\n".join(slice_)
        prefix = f"[runner] showing lines {s}-{s + len(slice_) - 1} of {len(lines)}\n"
        return prefix + text

    if truncated:
        text += f"\n[runner] file truncated to {cap} bytes ({size} bytes total)"
    return text
