"""commit_and_push tool: stage all changes, commit, push, optionally open PR."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from runner.github.client import create_pull_request
from runner.sandbox.shell import run_shell_capped

if TYPE_CHECKING:
    from runner.agent.context import RunContext


async def handle(ctx: "RunContext", args: dict[str, Any]) -> str:
    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        return "[runner] commit_and_push: 'message' is required"
    message = message.strip()

    rc, stdout, stderr = await run_shell_capped(
        ["git", "add", "-A"],
        cwd=ctx.workspace.root,
        timeout_seconds=60,
    )
    if rc != 0:
        return f"[runner] git add failed: {stderr}"

    # Bail out early if nothing to commit so we don't push empty commits.
    rc_chk, status_out, _ = await run_shell_capped(
        ["git", "status", "--porcelain"],
        cwd=ctx.workspace.root,
        timeout_seconds=15,
    )
    if rc_chk == 0 and not status_out.strip():
        return "[runner] nothing to commit (working tree clean)"

    rc, stdout, stderr = await run_shell_capped(
        ["git", "commit", "-m", message],
        cwd=ctx.workspace.root,
        timeout_seconds=60,
    )
    if rc != 0:
        return f"[runner] git commit failed: {stderr}"

    rc_sha, sha_out, _ = await run_shell_capped(
        ["git", "rev-parse", "HEAD"],
        cwd=ctx.workspace.root,
        timeout_seconds=15,
    )
    commit_sha = sha_out.strip() if rc_sha == 0 else None

    push_args = ["git", "push", "origin", ctx.workspace.branch]
    # Use --set-upstream the first time we push the agent branch; harmless if
    # it already tracks origin.
    if ctx.run_state.first_push:
        push_args.insert(2, "--set-upstream")
    rc, stdout, stderr = await run_shell_capped(
        push_args,
        cwd=ctx.workspace.root,
        timeout_seconds=180,
    )
    if rc != 0:
        return f"[runner] git push failed: {stderr}"

    ctx.run_state.first_push = False
    ctx.run_state.last_commit_sha = commit_sha

    pr_url: str | None = ctx.run_state.pr_url

    # Open the PR on the first successful push in PR mode (idempotent if it
    # already exists; GitHub will return 422 in that case which we ignore).
    if ctx.job.mode == "open_pr" and pr_url is None:
        try:
            data = await create_pull_request(
                token=ctx.github_token,
                repo_full_name=ctx.workspace.repo_full_name,
                head_branch=ctx.workspace.branch,
                base_branch=ctx.workspace.base_branch,
                title=_pr_title(ctx, message),
                body=_pr_body(ctx, message),
            )
            pr_url = data.get("html_url")
            ctx.run_state.pr_url = pr_url
        except Exception as exc:  # noqa: BLE001
            # Don't fail the run if PR opening hiccups — the push succeeded.
            await ctx.events.emit(
                "error",
                {"where": "create_pull_request", "error": str(exc)[:500]},
            )

    await ctx.events.emit(
        "commit",
        {
            "commit_sha": commit_sha,
            "message": message,
            "branch": ctx.workspace.branch,
            "base_branch": ctx.workspace.base_branch,
            "pr_url": pr_url,
            "mode": ctx.job.mode,
        },
    )

    suffix = ""
    if pr_url:
        suffix = f"\nPR: {pr_url}"
    return (
        f"[runner] committed {commit_sha or '(unknown sha)'} on "
        f"{ctx.workspace.branch} and pushed.{suffix}"
    )


def _pr_title(ctx: "RunContext", commit_message: str) -> str:
    title = ctx.job.context.get("task_title") if isinstance(ctx.job.context, dict) else None
    if isinstance(title, str) and title.strip():
        return f"[Continuum] {title.strip()}"
    return commit_message.splitlines()[0][:72]


def _pr_body(ctx: "RunContext", commit_message: str) -> str:
    parts = [
        "Opened by the Continuum agentic task completor.",
        f"- Task ID: {ctx.job.task_id}",
        f"- Run ID: {ctx.job.run_id}",
    ]
    instr = ctx.job.instructions
    if instr and instr.strip():
        parts.append("\nUser instructions:\n")
        parts.append(instr.strip())
    parts.append("\nLatest commit:\n")
    parts.append(commit_message)
    return "\n".join(parts)
