"""Tool registry: JSON schemas + a dispatcher mapping tool names to handlers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from runner.agent.tools import (  # noqa: E402  (registered as side effect)
    apply_patch as _apply_patch,
    commit_and_push as _commit_and_push,
    done as _done,
    git_diff as _git_diff,
    git_status as _git_status,
    glob_files as _glob_files,
    grep_files as _grep_files,
    list_dir as _list_dir,
    read_many_files as _read_many_files,
    read_file as _read_file,
    run_shell as _run_shell,
    write_file as _write_file,
)

if TYPE_CHECKING:
    from runner.agent.context import RunContext


Handler = Callable[["RunContext", dict[str, Any]], Coroutine[Any, Any, str]]


# OpenAI / LiteLLM tool schema. ``parameters`` follows JSON Schema. We keep
# descriptions tight so the model spends its budget on actual reasoning.

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": (
                "List files and subdirectories at a path inside the workspace. "
                "Use this to explore the repo structure."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path. Use '.' for the workspace root.",
                    },
                    "max_entries": {
                        "type": "integer",
                        "description": "Cap the number of returned entries. Default 200.",
                        "default": 200,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": (
                "Find tracked files by glob pattern using git's file list. "
                "Use this before reading files when you know a filename pattern."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Git glob pathspec, e.g. '**/*.tsx' or 'src/**/Task*.ts'.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Cap returned paths. Default 200.",
                        "default": 200,
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_files",
            "description": (
                "Search file contents with ripgrep and return compact path:line previews. "
                "Use path and glob to scope searches, and avoid repeating the same broad "
                "search before reading a concrete file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Ripgrep regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file or directory to search. Default '.'.",
                        "default": ".",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Optional ripgrep glob filter, e.g. '*.tsx'.",
                    },
                    "max_matches": {
                        "type": "integer",
                        "description": "Cap visible matches. Default 50, maximum 200.",
                        "default": 50,
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file inside the workspace. Always read the whole file "
                "in a single call (cap 200 KB) unless it is genuinely too large. "
                "Do NOT paginate with start_line/end_line for files under the cap. "
                "Prefer read_many_files when you need several files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                    "start_line": {
                        "type": "integer",
                        "description": "1-indexed first line to return (optional).",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "1-indexed last line to return inclusive (optional).",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_many_files",
            "description": (
                "Read several UTF-8 text files in one tool call. Prefer this over "
                "multiple read_file calls when exploring related files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Workspace-relative file paths to read.",
                    },
                    "max_bytes_each": {
                        "type": "integer",
                        "description": "Per-file byte cap. Default 50000.",
                        "default": 50000,
                    },
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or overwrite a text file inside the workspace with the "
                "given contents. Existing files are replaced atomically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "contents": {"type": "string"},
                },
                "required": ["path", "contents"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Apply a unified diff to the workspace via `git apply`. The patch "
                "must use workspace-relative paths."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "Unified diff text (the body of a `git diff` output).",
                    }
                },
                "required": ["patch"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run an allow-listed shell command in the workspace and return its "
                "stdout/stderr. Use this for `npm install`, `pytest`, `tsc`, etc. "
                "Network-egress and destructive commands are blocked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "Full shell command. Will be split with shlex; use "
                            "`bash -lc \"…\"` for piped/compound forms."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 120, max 600).",
                        "default": 120,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Run `git status --porcelain` to see the current working-tree state.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show pending changes via `git diff` (working tree by default).",
            "parameters": {
                "type": "object",
                "properties": {
                    "staged": {
                        "type": "boolean",
                        "description": "If true, show `git diff --cached`. Default false.",
                        "default": False,
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional workspace-relative path to limit the diff to.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commit_and_push",
            "description": (
                "Stage all changes, commit with the given message, and push to the "
                "active branch. In `open_pr` mode this also opens a PR back into "
                "the linked branch on the first call. Returns the resulting commit "
                "SHA and (for PR mode) the PR URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Commit message. First line is the subject.",
                    }
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": (
                "Finish the run. Always call this last with a short summary of the "
                "changes you made for the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "User-facing summary (markdown allowed).",
                    }
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    },
]


_HANDLERS: dict[str, Handler] = {
    "list_dir": _list_dir.handle,
    "glob_files": _glob_files.handle,
    "grep_files": _grep_files.handle,
    "read_file": _read_file.handle,
    "read_many_files": _read_many_files.handle,
    "write_file": _write_file.handle,
    "apply_patch": _apply_patch.handle,
    "run_shell": _run_shell.handle,
    "git_status": _git_status.handle,
    "git_diff": _git_diff.handle,
    "commit_and_push": _commit_and_push.handle,
    "done": _done.handle,
}


async def dispatch(ctx: "RunContext", name: str, raw_args: str) -> str:
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as exc:
        return f"[runner] tool args were not valid JSON: {exc}"
    if not isinstance(args, dict):
        return "[runner] tool args must be a JSON object"
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"[runner] unknown tool: {name!r}"
    try:
        return await handler(ctx, args)
    except Exception as exc:  # noqa: BLE001
        return f"[runner] tool {name!r} raised: {type(exc).__name__}: {exc}"


def format_user_visible_args(name: str, raw_args: str) -> dict[str, Any]:
    """
    Return a redacted version of args for the live UI feed (no huge file
    contents). The activity feed is allowed to show short args verbatim but
    bigger payloads are summarized to keep the SSE lean.
    """
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        return {"raw": (raw_args or "")[:200]}
    if not isinstance(args, dict):
        return {"raw": str(args)[:200]}
    cleaned: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 400:
            cleaned[k] = v[:400] + f"… ({len(v)} chars total)"
        else:
            cleaned[k] = v
    return cleaned
