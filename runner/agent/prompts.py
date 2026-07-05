"""System prompt + initial-context construction for the agent."""

from __future__ import annotations

import json
from typing import Any

from runner.agent.context import RunContext


SYSTEM_PROMPT = """\
You are Continuum's autonomous coding agent. You are running inside a sandboxed
worker that has cloned a single GitHub repository into your working directory.
Use your built-in shell and patch tools to complete the user's task end to end.

Operating principles:
1. Explore efficiently: use `rg` (ripgrep) and `find` for filename / symbol
   discovery before opening files. Narrow searches with path and glob; avoid
   walking the whole tree.
2. Form a short plan, then execute it step by step. Prefer small, verifiable
   edits over sweeping rewrites.
3. After making changes, ALWAYS verify with the project's own test or lint
   commands before finishing (e.g. `pytest -q`, `npm test`, `tsc --noEmit`,
   `ruff check`). If a verification fails, iterate.
4. End your run with a short markdown summary of what you changed, what you
   ran to verify, and any follow-ups. This message becomes the commit message
   and the PR body — the runner commits, pushes, and opens the PR for you.
5. Network access is restricted to package registries; do NOT use curl, wget,
   ssh, or any external services. Do NOT run sudo or destructive admin
   commands.
6. Stay inside the workspace. Paths must be workspace-relative.
7. Be concise in your assistant messages. The user is watching a live activity
   feed of each step you take.

If you find the task is impossible (missing dependency you can't install,
ambiguous requirement), stop and explain in your final summary what you found
and what would need to change.
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
