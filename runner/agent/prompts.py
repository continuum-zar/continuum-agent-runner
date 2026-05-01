"""System prompt + initial-context construction for the agent."""

from __future__ import annotations

import json
from typing import Any

from runner.agent.context import RunContext


SYSTEM_PROMPT = """\
You are Continuum's autonomous coding agent. You are running inside a sandboxed
worker that has cloned a single GitHub repository into a working directory. You
have a small set of tools (list_dir, glob_files, grep_files, read_file,
read_many_files, write_file, apply_patch, run_shell, git_status, git_diff,
commit_and_push, done) and you must use them to complete the user's task.

Operating principles:
1. Start by exploring efficiently: use glob_files for filename patterns and
   grep_files for symbols/text before reading files. After one broad grep pass,
   scope follow-up searches with path and glob. Use read_many_files for related
   files. Avoid list_dir walks unless you need broad repo shape.
2. Form a short plan, then execute it step by step. Prefer small, verifiable
   edits over sweeping rewrites, and combine related reads/searches into the
   fewest useful tool calls.
3. After making changes, ALWAYS verify with the project's own test or lint
   commands before committing (e.g. `pytest -q`, `npm test`, `tsc --noEmit`,
   `ruff check`). If a verification fails, iterate.
4. Commit when you have a coherent, working set of changes. Use a clear
   commit message in the imperative mood. The first call to commit_and_push
   in `open_pr` mode also opens a PR.
5. When you are completely finished, call the `done` tool with a short
   markdown summary of what you did, what you tested, and any follow-ups.
6. Network access is restricted to package registries; do NOT try to use curl,
   wget, ssh, or any external services. Do NOT run sudo or destructive admin
   commands — they will be rejected.
7. Stay inside the workspace. Paths must be workspace-relative.
8. Be concise in your messages. The user is watching a live activity feed of
   each tool call you make.
9. Do not page through one file in slices. For files under the read cap, read
   the whole file once; for multiple files, use read_many_files.

If a tool returns an error, read the error carefully and adjust. If you find
the task is impossible (missing dependency the agent can't install, ambiguous
requirement), call `done` with a clear summary explaining what you found.
If a tool result starts with `[runner] repeated tool call skipped` or
`[runner] duplicate`, you are in a loop. Do NOT retry the same call. Switch
tool (try `glob_files` for filenames, `list_dir` for shape, or `read_file` on
a specific path), narrow searches with path/glob, or call `done` with what you
have.
"""


def build_initial_messages(ctx: RunContext) -> list[dict[str, Any]]:
    job_ctx = ctx.job.context if isinstance(ctx.job.context, dict) else {}

    task_section_lines = [
        f"# Task #{ctx.job.task_id}",
    ]
    title = job_ctx.get("task_title")
    if isinstance(title, str) and title:
        task_section_lines.append(f"\n**Title:** {title}")
    description = job_ctx.get("task_description")
    if isinstance(description, str) and description:
        task_section_lines.append(f"\n**Description:**\n{description}")
    checklists = job_ctx.get("checklists")
    if isinstance(checklists, list) and checklists:
        task_section_lines.append("\n**Checklist:**")
        for item in checklists[:50]:
            if not isinstance(item, dict):
                continue
            done = "x" if item.get("done") else " "
            text = item.get("text", "")
            task_section_lines.append(f"- [{done}] {text}")

    extras: list[str] = []
    priority = job_ctx.get("priority")
    if priority:
        extras.append(f"priority={priority}")
    scope = job_ctx.get("scope_weight")
    if scope:
        extras.append(f"scope={scope}")
    due = job_ctx.get("due_date")
    if due:
        extras.append(f"due={due}")
    if extras:
        task_section_lines.append("\n_" + ", ".join(extras) + "_")

    instructions = ctx.job.instructions
    if instructions and instructions.strip():
        task_section_lines.append("\n**Additional user instructions for this build:**")
        task_section_lines.append(instructions.strip())

    repo_section_lines = [
        "# Repository",
        f"- Repo: `{ctx.job.linked_repo}`",
        f"- Linked branch (target): `{ctx.workspace.base_branch}`",
        f"- Active working branch: `{ctx.workspace.branch}`",
        f"- Mode: `{ctx.job.mode}` (commit_and_push will "
        + ("open a PR back into the linked branch" if ctx.job.mode == "open_pr" else "push directly")
        + ")",
        f"- Workspace root: `{ctx.workspace.root}` (all paths are workspace-relative)",
    ]

    repo_overview = job_ctx.get("repo_overview")
    if isinstance(repo_overview, str) and repo_overview.strip():
        repo_section_lines.append("\n## Repo overview from indexer\n")
        repo_section_lines.append(repo_overview.strip())

    rag_chunks = job_ctx.get("rag_chunks")
    rag_section: list[str] = []
    if isinstance(rag_chunks, list) and rag_chunks:
        rag_section.append("# Related context (from RAG)\n")
        for chunk in rag_chunks[:10]:
            if not isinstance(chunk, dict):
                continue
            label = chunk.get("label") or chunk.get("source") or "snippet"
            content = chunk.get("content") or ""
            if not isinstance(content, str):
                continue
            rag_section.append(f"### {label}\n{content[:1500]}")

    comments = job_ctx.get("recent_comments")
    comments_section: list[str] = []
    if isinstance(comments, list) and comments:
        comments_section.append("# Recent task comments\n")
        for c in comments[:10]:
            if not isinstance(c, dict):
                continue
            author = c.get("author") or c.get("author_name") or "user"
            body = c.get("body") or c.get("text") or ""
            if not isinstance(body, str):
                continue
            comments_section.append(f"- **{author}:** {body[:500]}")

    user_message = "\n\n".join(
        block
        for block in (
            "\n".join(task_section_lines),
            "\n".join(repo_section_lines),
            "\n".join(rag_section) if rag_section else "",
            "\n".join(comments_section) if comments_section else "",
            "Begin by exploring the repo efficiently with glob_files, grep_files, "
            "and read_many_files, then plan and implement.",
        )
        if block
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]


def serialize_for_log(messages: list[dict[str, Any]]) -> str:
    """Compact debug representation for logs."""
    return json.dumps(
        [
            {
                "role": m.get("role"),
                "content_preview": (
                    (m.get("content") or "")[:200]
                    if isinstance(m.get("content"), str)
                    else str(m.get("content"))[:200]
                ),
                "tool_calls": bool(m.get("tool_calls")),
            }
            for m in messages
        ]
    )
