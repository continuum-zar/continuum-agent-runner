"""grep_files tool: fast repo text search via ripgrep."""

from __future__ import annotations

import asyncio
import fnmatch
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from runner.sandbox.shell import ShellPolicyError, run_shell_capped
from runner.sandbox.workspace import WorkspacePathError

if TYPE_CHECKING:
    from runner.agent.context import RunContext

_FALLBACK_SKIP_DIRS = {".git", "node_modules", ".next", "dist", "build"}
_FALLBACK_MAX_FILES = 5_000
_FALLBACK_MAX_FILE_BYTES = 1_000_000
_BINARY_SAMPLE_BYTES = 2_048
_MAX_LINE_COLUMNS = 240


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

    try:
        rc, stdout, stderr = await run_shell_capped(
            argv,
            cwd=ctx.workspace.root,
            timeout_seconds=30,
        )
    except (FileNotFoundError, ShellPolicyError):
        return await _python_grep_fallback(ctx, target, pattern, glob, max_matches)

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


async def _python_grep_fallback(
    ctx: "RunContext",
    target: Path,
    pattern: str,
    glob: Any,
    max_matches: int,
) -> str:
    return await asyncio.to_thread(
        _python_grep_fallback_sync,
        ctx,
        target,
        pattern,
        glob if isinstance(glob, str) and glob else None,
        max_matches,
    )


def _python_grep_fallback_sync(
    ctx: "RunContext",
    target: Path,
    pattern: str,
    glob: str | None,
    max_matches: int,
) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"[runner] grep_files: invalid regex pattern: {exc}"

    matches: list[str] = []
    scanned_files = 0
    truncated = False

    for file_path in _iter_candidate_files(target):
        rel_path = ctx.workspace.relative(file_path)
        if glob and not _matches_glob(rel_path, file_path.name, glob):
            continue

        scanned_files += 1
        if scanned_files > _FALLBACK_MAX_FILES:
            truncated = True
            break

        for line_no, line in _matching_lines(file_path, regex):
            matches.append(f"{rel_path}:{line_no}:{line[:_MAX_LINE_COLUMNS]}")
            if len(matches) >= max_matches:
                truncated = True
                break
        if len(matches) >= max_matches:
            break

    note = "[runner] note: rg unavailable, used Python fallback"
    if not matches:
        out = f"{note}\n[runner] no matches"
    else:
        out = f"{note}\n" + "\n".join(matches)
    if truncated:
        out += f"\n[runner] matches truncated at {len(matches)} visible matches"
    return out


def _iter_candidate_files(target: Path) -> Iterator[Path]:
    if target.is_file():
        yield target
        return

    for path in target.rglob("*"):
        if path.is_dir():
            continue
        if any(part in _FALLBACK_SKIP_DIRS for part in path.parts):
            continue
        yield path


def _matches_glob(rel_path: str, file_name: str, glob: str) -> bool:
    return fnmatch.fnmatch(rel_path, glob) or fnmatch.fnmatch(file_name, glob)


def _matching_lines(file_path: Path, regex: re.Pattern[str]) -> list[tuple[int, str]]:
    try:
        with file_path.open("rb") as fh:
            sample = fh.read(_BINARY_SAMPLE_BYTES)
            if b"\0" in sample:
                return []
            remainder = fh.read(max(0, _FALLBACK_MAX_FILE_BYTES - len(sample)))
            if fh.read(1):
                return []
    except OSError:
        return []

    text = (sample + remainder).decode("utf-8", errors="replace")
    return [
        (line_no, line.rstrip("\n\r"))
        for line_no, line in enumerate(text.splitlines(), start=1)
        if regex.search(line)
    ]
