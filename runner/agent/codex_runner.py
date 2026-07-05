"""Drives a Codex CLI subprocess for one agent run.

Replaces the homegrown LiteLLM tool loop in `runner.agent.loop`. Same
`run_agent(ctx) -> AgentRunResult` signature so `orchestrator.py` doesn't
care which backend is in use.

Codex does the work inside the cloned workspace; the wrapper handles the
parts Codex must not touch: pushing to the remote and opening the PR.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from typing import Any, Optional

from runner.agent.context import RunContext
from runner.agent.prompts import build_initial_messages
from runner.config import settings
from runner.github.client import create_pull_request
from runner.logger import get_logger
from runner.models import AgentRunResult
from runner.sandbox.shell import run_shell_capped

logger = get_logger(__name__)


_NO_PUSH_RIDER = """\

# Hard constraints from the runner
- Do NOT run `git push`, `git remote`, `gh pr create`, or anything that touches
  the remote. The runner will commit and push your changes after you finish.
- Do NOT run `sudo`, `curl`, `wget`, `ssh`, or anything that talks to the
  network beyond package registries.
- Stay inside the workspace root. All paths are workspace-relative.
- When you are done, stop. Write a short final summary as your last assistant
  message — that becomes the commit message and the PR body.
"""


def _build_codex_prompt(ctx: RunContext) -> str:
    messages = build_initial_messages(ctx)
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in messages if m["role"] == "user"), "")
    return f"{system}\n\n{user}{_NO_PUSH_RIDER}"


# ---------------------------------------------------------------------------
# Codex JSON event mapping
# ---------------------------------------------------------------------------

_seen_unknown_types: set[str] = set()
_seen_unknown_item_types: set[str] = set()


async def _handle_codex_event(ctx: RunContext, evt: dict[str, Any]) -> Optional[str]:
    """
    Map one Codex JSON event onto an EventPublisher.emit call.

    Codex 0.136.0 emits:
      - thread.started     : {thread_id}
      - turn.started       : {}
      - turn.completed     : {usage: {input_tokens, output_tokens, ...}}
      - item.completed     : {item: {id, type, ...}}  -- the real work
      - error              : {message}
    where item.type is one of agent_message / agent_reasoning /
    command_execution / file_change / etc.
    """
    et = evt.get("type") or ""

    if et == "thread.started":
        await ctx.events.emit(
            "status",
            {"phase": "codex_thread_started", "thread_id": evt.get("thread_id")},
        )
        return None

    if et == "turn.started":
        return None

    if et == "turn.completed":
        usage = evt.get("usage") or {}
        try:
            total = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            ctx.tokens_used += total
        except (TypeError, ValueError):
            pass
        return None

    if et == "error":
        await ctx.events.emit(
            "error",
            {"error": str(evt.get("message") or evt.get("error") or evt)[:500]},
        )
        return None

    if et == "item.completed":
        return await _handle_item(ctx, evt.get("item") or {})

    if et and et not in _seen_unknown_types:
        _seen_unknown_types.add(et)
        logger.info("codex.unknown_event_type", type=et, sample=_preview(evt))
    return None


async def _handle_item(ctx: RunContext, item: dict[str, Any]) -> Optional[str]:
    it = item.get("type") or ""

    if it == "agent_message":
        text = item.get("text") or item.get("content") or ""
        if isinstance(text, str) and text.strip():
            ctx.iterations += 1
            await ctx.events.emit(
                "thinking",
                {"step": ctx.iterations, "text": text[:2000]},
            )
            return text.strip()
        return None

    if it == "agent_reasoning":
        text = item.get("text") or item.get("content") or ""
        if isinstance(text, str) and text.strip():
            await ctx.events.emit(
                "thinking",
                {"step": ctx.iterations + 1, "text": text[:2000]},
            )
        return None

    if it == "command_execution":
        cmd = item.get("command") or item.get("argv") or item.get("cmd") or ""
        await ctx.events.emit(
            "tool_call",
            {"name": "shell", "args": _preview(cmd)},
        )
        output = (
            item.get("aggregated_output")
            or item.get("output")
            or item.get("stdout")
            or ""
        )
        if output:
            await ctx.events.emit(
                "shell_stdout",
                {"chunk": str(output)[:4000]},
            )
        exit_code = item.get("exit_code")
        await ctx.events.emit(
            "tool_result",
            {
                "name": "shell",
                "exit_code": exit_code,
                "result_preview": str(output)[:1500],
            },
        )
        return None

    if it in ("file_change", "patch_apply", "apply_patch"):
        files = item.get("files") or item.get("paths") or []
        await ctx.events.emit(
            "tool_call",
            {"name": "apply_patch", "args": _preview(files or item)},
        )
        await ctx.events.emit(
            "tool_result",
            {"name": "apply_patch", "result_preview": _preview(item)},
        )
        return None

    if it == "todo_list":
        await ctx.events.emit(
            "status",
            {"phase": "codex_plan", "items": item.get("items") or []},
        )
        return None

    if it and it not in _seen_unknown_item_types:
        _seen_unknown_item_types.add(it)
        logger.info("codex.unknown_item_type", item_type=it, sample=_preview(item))
    return None


def _preview(value: Any) -> str:
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value)[:200]
        except (TypeError, ValueError):
            return str(value)[:200]
    return str(value)[:200]


# ---------------------------------------------------------------------------
# Subprocess lifecycle
# ---------------------------------------------------------------------------

async def _spawn_codex(ctx: RunContext, prompt: str) -> asyncio.subprocess.Process:
    # Each run already lives in an isolated per-run workspace dir owned by the
    # runner; in production we're inside a Docker container on top of that.
    # Codex's own bwrap sandbox can't always run (host-cap dependent) and would
    # be redundant, so we hand it full access to its working dir.
    cmd = [
        settings.CODEX_BIN or "codex",
        "exec",
        "--json",
        "--sandbox", settings.CODEX_SANDBOX,
        "--cd", str(ctx.workspace.root),
        "--model", settings.LLM_MODEL,
        prompt,
    ]
    env = os.environ.copy()
    if settings.LLM_API_KEY:
        env["OPENAI_API_KEY"] = settings.LLM_API_KEY

    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(ctx.workspace.root),
        env=env,
    )


async def _cancel_watcher(
    ctx: RunContext,
    proc: asyncio.subprocess.Process,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        if ctx.run_state.cancel_requested and proc.returncode is None:
            logger.info("codex.cancel_signal_sent", run_id=ctx.job.run_id)
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def _drain_stderr(proc: asyncio.subprocess.Process) -> str:
    assert proc.stderr is not None
    chunks: list[bytes] = []
    async for line in proc.stderr:
        chunks.append(line)
    return b"".join(chunks).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Post-run: commit + push + PR (the part Codex isn't allowed to do)
# ---------------------------------------------------------------------------

async def _commit_push_pr(ctx: RunContext, summary: str) -> None:
    rc, status_out, _ = await run_shell_capped(
        ["git", "status", "--porcelain"],
        cwd=ctx.workspace.root,
        timeout_seconds=15,
    )
    if rc != 0 or not status_out.strip():
        ctx.run_state.summary = summary or "No changes were necessary."
        return

    title = _commit_title(ctx, summary)
    body = (summary or "").strip() or title
    message = f"{title}\n\n{body}"

    for cmd, t in (
        (["git", "add", "-A"], 60),
        (["git", "commit", "-m", message], 60),
    ):
        rc, _, stderr = await run_shell_capped(
            cmd, cwd=ctx.workspace.root, timeout_seconds=t,
        )
        if rc != 0:
            raise RuntimeError(f"git {cmd[1]} failed: {stderr[:300]}")

    rc, sha_out, _ = await run_shell_capped(
        ["git", "rev-parse", "HEAD"],
        cwd=ctx.workspace.root,
        timeout_seconds=15,
    )
    commit_sha = sha_out.strip() if rc == 0 else None

    rc, _, stderr = await run_shell_capped(
        ["git", "push", "--set-upstream", "origin", ctx.workspace.branch],
        cwd=ctx.workspace.root,
        timeout_seconds=180,
    )
    if rc != 0:
        raise RuntimeError(f"git push failed: {stderr[:300]}")

    ctx.run_state.last_commit_sha = commit_sha
    ctx.run_state.summary = summary

    pr_url: Optional[str] = None
    if ctx.job.mode == "open_pr":
        try:
            data = await create_pull_request(
                token=ctx.github_token,
                repo_full_name=ctx.workspace.repo_full_name,
                head_branch=ctx.workspace.branch,
                base_branch=ctx.workspace.base_branch,
                title=title,
                body=_pr_body(ctx, summary),
            )
            pr_url = data.get("html_url")
            ctx.run_state.pr_url = pr_url
        except Exception as exc:  # noqa: BLE001
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


def _commit_title(ctx: RunContext, summary: str) -> str:
    task_title = (
        ctx.job.context.get("task_title")
        if isinstance(ctx.job.context, dict)
        else None
    )
    if isinstance(task_title, str) and task_title.strip():
        return f"[Continuum] {task_title.strip()[:72]}"
    first_line = (summary or "Agent changes").splitlines()[0]
    return first_line[:72] or "Agent changes"


def _pr_body(ctx: RunContext, summary: str) -> str:
    parts = [
        "Opened by the Continuum agentic task completor (Codex backend).",
        f"- Task ID: {ctx.job.task_id}",
        f"- Run ID: {ctx.job.run_id}",
    ]
    if ctx.job.instructions and ctx.job.instructions.strip():
        parts += ["\nUser instructions:\n", ctx.job.instructions.strip()]
    if summary and summary.strip():
        parts += ["\nSummary:\n", summary.strip()]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Entry point — same signature as the old run_agent
# ---------------------------------------------------------------------------

async def run_agent(ctx: RunContext) -> AgentRunResult:
    deadline = time.monotonic() + settings.MAX_WALL_CLOCK_SECONDS
    # Kill below the real cap so a one-turn overshoot still lands under budget.
    token_kill_threshold = max(
        0, settings.MAX_TOKENS_PER_RUN - settings.TOKEN_BUDGET_HEADROOM
    )
    prompt = _build_codex_prompt(ctx)

    await ctx.events.emit(
        "status",
        {
            "phase": "codex_started",
            "model": settings.LLM_MODEL,
            "workspace": str(ctx.workspace.root),
        },
    )

    proc = await _spawn_codex(ctx, prompt)
    stop_watcher = asyncio.Event()
    watcher = asyncio.create_task(_cancel_watcher(ctx, proc, stop_watcher))
    stderr_task = asyncio.create_task(_drain_stderr(proc))

    last_summary: str = ""
    error_msg: Optional[str] = None

    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            if time.monotonic() > deadline:
                error_msg = (
                    f"wall-clock budget exceeded ({settings.MAX_WALL_CLOCK_SECONDS}s)"
                )
                try:
                    proc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
                break

            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                await ctx.events.emit("shell_stdout", {"chunk": line[:4000]})
                continue

            maybe_text = await _handle_codex_event(ctx, evt)
            if maybe_text:
                last_summary = maybe_text

            # Token budget guard. ctx.tokens_used is updated on every
            # turn.completed event above; reap the run once it crosses the
            # headroom-adjusted threshold so a runaway task can't burn unbounded
            # spend and a final turn can't blow past the real cap.
            if ctx.tokens_used > token_kill_threshold:
                error_msg = (
                    f"token budget exceeded ({ctx.tokens_used} tokens > "
                    f"{token_kill_threshold} kill threshold; cap "
                    f"{settings.MAX_TOKENS_PER_RUN} - {settings.TOKEN_BUDGET_HEADROOM} "
                    f"headroom)"
                )
                try:
                    proc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
                break

        rc = await proc.wait()
        stderr_text = await stderr_task

        if ctx.run_state.cancel_requested:
            error_msg = "cancelled"
        elif rc != 0 and not error_msg:
            error_msg = f"codex exited with rc={rc}: {stderr_text[:500]}"

    finally:
        stop_watcher.set()
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    if error_msg:
        await ctx.events.emit("error", {"error": error_msg})
        return AgentRunResult(
            status="cancelled" if error_msg == "cancelled" else "failed",
            error=error_msg,
            summary=last_summary or None,
            agent_branch=ctx.workspace.branch if ctx.job.mode == "open_pr" else None,
            iterations=ctx.iterations,
            tokens_used=ctx.tokens_used,
        )

    try:
        await _commit_push_pr(ctx, last_summary)
    except Exception as exc:  # noqa: BLE001
        await ctx.events.emit(
            "error", {"where": "post_run_commit", "error": str(exc)[:500]},
        )
        return AgentRunResult(
            status="failed",
            error=f"post_run_commit_failed: {exc}",
            summary=last_summary or None,
            agent_branch=ctx.workspace.branch if ctx.job.mode == "open_pr" else None,
            iterations=ctx.iterations,
            tokens_used=ctx.tokens_used,
        )

    final = ctx.run_state.summary or last_summary or "Run completed."
    await ctx.events.emit("final_message", {"text": final})

    return AgentRunResult(
        status="succeeded",
        summary=final,
        agent_branch=ctx.workspace.branch if ctx.job.mode == "open_pr" else None,
        commit_sha=ctx.run_state.last_commit_sha,
        pr_url=ctx.run_state.pr_url,
        iterations=ctx.iterations,
        tokens_used=ctx.tokens_used,
    )
