"""read_many_files tool: bundle several workspace file reads into one turn."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from runner.config import settings
from runner.sandbox.workspace import WorkspacePathError

if TYPE_CHECKING:
    from runner.agent.context import RunContext


async def handle(ctx: "RunContext", args: dict[str, Any]) -> str:
    paths = args.get("paths")
    if not isinstance(paths, list) or not paths:
        return "[runner] read_many_files: 'paths' must be a non-empty array"

    try:
        max_bytes_each = int(args.get("max_bytes_each", 50_000) or 50_000)
    except (TypeError, ValueError):
        max_bytes_each = 50_000
    max_bytes_each = max(1_000, min(max_bytes_each, settings.MAX_FILE_BYTES))

    max_files = 20
    out: list[str] = []
    total_bytes = 0

    for raw_path in paths[:max_files]:
        if not isinstance(raw_path, str) or not raw_path:
            out.append(
                "--- BEGIN <invalid path> ---\n"
                "[runner] path must be a non-empty string\n"
                "--- END <invalid path> ---"
            )
            continue

        try:
            target = ctx.workspace.resolve(raw_path)
        except WorkspacePathError as exc:
            out.append(f"--- BEGIN {raw_path} ---\n[runner] {exc}\n--- END {raw_path} ---")
            continue
        if not target.exists():
            out.append(
                f"--- BEGIN {raw_path} ---\n"
                "[runner] file does not exist\n"
                f"--- END {raw_path} ---"
            )
            continue
        if not target.is_file():
            out.append(
                f"--- BEGIN {raw_path} ---\n[runner] not a regular file\n--- END {raw_path} ---"
            )
            continue

        try:
            size = target.stat().st_size
            with target.open("rb") as fh:
                data = fh.read(min(size, max_bytes_each))
        except OSError as exc:
            out.append(
                f"--- BEGIN {raw_path} ---\n"
                f"[runner] could not read file: {exc}\n"
                f"--- END {raw_path} ---"
            )
            continue

        total_bytes += len(data)
        text = data.decode("utf-8", errors="replace")
        rel = ctx.workspace.relative(target)
        header = f"--- BEGIN {rel} ({size} bytes) ---"
        footer = f"--- END {rel} ---"
        if size > max_bytes_each:
            text += (
                f"\n[runner] file truncated to {max_bytes_each} bytes "
                f"({size} bytes total)"
            )
        out.append(f"{header}\n{text}\n{footer}")

        if total_bytes >= settings.MAX_FILE_BYTES:
            out.append(
                f"[runner] read_many_files total output capped near {settings.MAX_FILE_BYTES} bytes"
            )
            break

    if len(paths) > max_files:
        out.append(f"[runner] path list truncated at {max_files} files")

    return "\n\n".join(out)
