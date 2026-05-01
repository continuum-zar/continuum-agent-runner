"""The homegrown LiteLLM tool-calling loop."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import litellm

from runner.agent.context import RunContext
from runner.agent.prompts import build_initial_messages
from runner.agent.tools import TOOL_SCHEMAS, dispatch, format_user_visible_args
from runner.config import settings
from runner.logger import get_logger
from runner.models import AgentRunResult

logger = get_logger(__name__)


# Configure LiteLLM globally — the API service uses LiteLLM the same way.
litellm.drop_params = True
litellm.suppress_debug_info = True


def _model_id() -> str:
    provider = (settings.LLM_PROVIDER or "openai").strip().lower()
    model = settings.LLM_MODEL.strip()
    if provider in ("openai", "azure", ""):
        return model
    # LiteLLM expects e.g. "anthropic/claude-3-5-sonnet" for non-OpenAI.
    if "/" in model:
        return model
    return f"{provider}/{model}"


def _api_key_kwargs() -> dict[str, str]:
    if not settings.LLM_API_KEY:
        return {}
    return {"api_key": settings.LLM_API_KEY}


async def run_agent(ctx: RunContext) -> AgentRunResult:
    """
    Drive the agent until it calls `done`, hits the iteration cap, runs out of
    wall-clock time, blows the token budget, or is cancelled.
    """
    messages: list[dict[str, Any]] = build_initial_messages(ctx)

    deadline = time.monotonic() + settings.MAX_WALL_CLOCK_SECONDS
    model = _model_id()

    await ctx.events.emit(
        "status",
        {"phase": "agent_loop_started", "model": model, "max_iterations": settings.MAX_ITERATIONS},
    )

    iterations = 0
    final_message: str | None = None
    error_msg: str | None = None

    while True:
        if ctx.run_state.cancel_requested:
            error_msg = "cancelled"
            break

        if time.monotonic() > deadline:
            error_msg = (
                f"wall-clock budget exceeded ({settings.MAX_WALL_CLOCK_SECONDS}s)"
            )
            break

        if iterations >= settings.MAX_ITERATIONS:
            error_msg = f"max iterations reached ({settings.MAX_ITERATIONS})"
            break

        if ctx.tokens_used >= settings.MAX_TOKENS_PER_RUN:
            error_msg = f"token budget exceeded ({settings.MAX_TOKENS_PER_RUN})"
            break

        iterations += 1
        ctx.iterations = iterations
        await ctx.events.emit("thinking", {"step": iterations})

        try:
            resp = await asyncio.wait_for(
                litellm.acompletion(
                    model=model,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                    temperature=0.2,
                    **_api_key_kwargs(),
                ),
                timeout=180,
            )
        except asyncio.TimeoutError:
            error_msg = "LLM call timed out (180s)"
            break
        except Exception as exc:  # noqa: BLE001
            error_msg = f"LLM call raised: {type(exc).__name__}: {exc}"
            break

        usage = getattr(resp, "usage", None)
        if usage is not None:
            try:
                ctx.tokens_used += int(getattr(usage, "total_tokens", 0) or 0)
            except (TypeError, ValueError):
                pass

        choice = resp.choices[0]
        msg = choice.message
        # Normalize to dict the way LiteLLM expects on the next turn.
        msg_dict: dict[str, Any] = {
            "role": "assistant",
            "content": getattr(msg, "content", None),
        }
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in tool_calls
            ]
        messages.append(msg_dict)

        if not tool_calls:
            # The model returned a plain assistant message with no tool call.
            # Treat it as the final answer if there is text content; otherwise
            # nudge it back into action by appending a reminder.
            content = msg_dict.get("content")
            if isinstance(content, str) and content.strip():
                final_message = content.strip()
                ctx.run_state.is_done = True
                ctx.run_state.summary = ctx.run_state.summary or final_message
                await ctx.events.emit("final_message", {"text": final_message})
                break
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "You did not call a tool. Either continue with another "
                        "tool call or call `done` with a summary."
                    ),
                }
            )
            continue

        # Execute each tool call and append the observation in the order the
        # model produced them.
        for tc in tool_calls:
            name = tc.function.name
            raw_args = tc.function.arguments or "{}"
            await ctx.events.emit(
                "tool_call",
                {"name": name, "args": format_user_visible_args(name, raw_args)},
            )
            observation = await dispatch(ctx, name, raw_args)
            await ctx.events.emit(
                "tool_result",
                {"name": name, "result_preview": observation[:1500]},
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": observation,
                }
            )
            if ctx.run_state.is_done:
                break

        if ctx.run_state.is_done:
            break

    # ------------------------------------------------------------------
    # Build the final result
    # ------------------------------------------------------------------
    if error_msg:
        await ctx.events.emit("error", {"error": error_msg})
        return AgentRunResult(
            status="cancelled" if error_msg == "cancelled" else "failed",
            error=error_msg,
            summary=ctx.run_state.summary or final_message,
            agent_branch=ctx.workspace.branch if ctx.job.mode == "open_pr" else None,
            commit_sha=ctx.run_state.last_commit_sha,
            pr_url=ctx.run_state.pr_url,
            iterations=iterations,
            tokens_used=ctx.tokens_used,
        )

    summary = ctx.run_state.summary or final_message or "Run completed."
    return AgentRunResult(
        status="succeeded",
        summary=summary,
        agent_branch=ctx.workspace.branch if ctx.job.mode == "open_pr" else None,
        commit_sha=ctx.run_state.last_commit_sha,
        pr_url=ctx.run_state.pr_url,
        iterations=iterations,
        tokens_used=ctx.tokens_used,
    )
