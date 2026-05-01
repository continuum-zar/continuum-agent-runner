"""write_file tool: atomically create/overwrite a file inside the workspace."""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING, Any

from runner.config import settings
from runner.sandbox.workspace import WorkspacePathError

if TYPE_CHECKING:
    from runner.agent.context import RunContext


async def handle(ctx: "RunContext", args: dict[str, Any]) -> str:
    path = args.get("path")
    contents = args.get("contents")
    if not isinstance(path, str) or not path:
        return "[runner] write_file: 'path' is required"
    if not isinstance(contents, str):
        return "[runner] write_file: 'contents' must be a string"

    encoded = contents.encode("utf-8")
    if len(encoded) > settings.MAX_FILE_BYTES:
        return (
            f"[runner] write_file refused: contents are {len(encoded)} bytes, "
            f"limit is {settings.MAX_FILE_BYTES}"
        )

    try:
        target = ctx.workspace.resolve(path)
    except WorkspacePathError as exc:
        return f"[runner] {exc}"

    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".agent_tmp_", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
        os.replace(tmp_path, target)
    except Exception as exc:  # noqa: BLE001
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return f"[runner] write failed: {exc}"

    rel = ctx.workspace.relative(target)
    return f"[runner] wrote {len(encoded)} bytes to {rel}"
