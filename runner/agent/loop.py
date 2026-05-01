"""The homegrown LiteLLM tool-calling loop."""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from typing import Any

import litellm
from litellm.exceptions import RateLimitError

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


def _model_id(model_name: str | None = None) -> str:
    provider = (settings.LLM_PROVIDER or "openai").strip().lower()
    model = (model_name or settings.LLM_MODEL).strip()
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


def _csv_set(value: str) -> set[str]:
    return {part.strip() for part in value.split(",") if part.strip()}


def _message_content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return str(content)


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    # Good enough for compaction thresholds; avoids provider tokenizer costs.
    chars = 0
    for message in messages:
        chars += len(_message_content_text(message))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            chars += len(str(tool_calls))
    return chars // 4


def _tool_call_lookup(messages: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    lookup: dict[str, tuple[str, str]] = {}
    for message in messages:
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            tool_call_id = tool_call.get("id")
            function = tool_call.get("function") or {}
            if not isinstance(tool_call_id, str) or not isinstance(function, dict):
                continue
            name = str(function.get("name") or "tool")
            args = str(function.get("arguments") or "{}")
            lookup[tool_call_id] = (name, args[:160])
    return lookup


def _is_tool_exception_observation(observation: str) -> bool:
    return observation.startswith("[runner] tool ") and " raised:" in observation


def _repeated_tool_call_observation() -> str:
    return (
        "[runner] repeated tool call skipped after "
        f"{settings.MAX_REPEATED_TOOL_CALLS} identical calls; use a narrower "
        "path/glob/pattern or read a specific file"
    )


def _dedupe_tool_observation(
    *,
    observation: str,
    name: str,
    raw_args: str,
    seen_observations: dict[str, tuple[str, str]],
) -> str:
    digest = hashlib.sha256(observation.encode("utf-8", errors="replace")).hexdigest()
    previous = seen_observations.get(digest)
    if previous is None:
        seen_observations[digest] = (name, raw_args[:160])
        return observation

    previous_name, previous_args = previous
    preview_chars = max(0, settings.DUPLICATE_TOOL_RESULT_PREVIEW_CHARS)
    preview = observation[:preview_chars].replace("\n", " ")
    return (
        f"[runner] duplicate {name} result omitted from history; identical to "
        f"earlier {previous_name} result for {previous_args}. "
        f"Preview: {preview}"
    )


def _compact_history_if_needed(messages: list[dict[str, Any]]) -> int:
    if _estimate_tokens(messages) <= settings.HISTORY_COMPACT_TOKEN_THRESHOLD:
        return 0

    tool_indices = [i for i, message in enumerate(messages) if message.get("role") == "tool"]
    keep_recent = max(0, settings.HISTORY_KEEP_RECENT_TOOL_RESULTS)
    compactable = set(tool_indices[:-keep_recent] if keep_recent else tool_indices)
    lookup = _tool_call_lookup(messages)

    compacted = 0
    for i in sorted(compactable):
        content = _message_content_text(messages[i])
        if len(content) <= 1024 or content.startswith("[runner] [compacted earlier"):
            continue
        tool_call_id = messages[i].get("tool_call_id")
        tool_name, args_preview = lookup.get(str(tool_call_id), ("tool", "{}"))
        messages[i]["content"] = (
            f"[runner] [compacted earlier {tool_name} result for {args_preview}; "
            "re-run the tool if you still need the contents]"
        )
        compacted += 1
    return compacted


async def run_agent(ctx: RunContext) -> AgentRunResult:
    """
    Drive the agent until it calls `done`, hits the iteration cap, runs out of
    wall-clock time, blows the token budget, or is cancelled.
    """
    messages: list[dict[str, Any]] = build_initial_messages(ctx)

    deadline = time.monotonic() + settings.MAX_WALL_CLOCK_SECONDS
    explore_model = _model_id(settings.LLM_EXPLORE_MODEL or settings.LLM_MODEL)
    synthesize_model = _model_id(settings.LLM_SYNTHESIZE_MODEL or settings.LLM_MODEL)
    synthesize_tools = _csv_set(settings.LLM_SYNTHESIZE_TOOLS)
    mode = "explore"

    await ctx.events.emit(
        "status",
        {
            "phase": "agent_loop_started",
            "model": explore_model,
            "synthesize_model": synthesize_model,
            "max_iterations": settings.MAX_ITERATIONS,
            "max_tokens_per_run": settings.MAX_TOKENS_PER_RUN,
            "history_compact_token_threshold": settings.HISTORY_COMPACT_TOKEN_THRESHOLD,
            "history_keep_recent_tool_results": settings.HISTORY_KEEP_RECENT_TOOL_RESULTS,
            "max_repeated_tool_calls": settings.MAX_REPEATED_TOOL_CALLS,
        },
    )

    iterations = 0
    final_message: str | None = None
    error_msg: str | None = None
    rate_limit_retries = 0
    last_failure_key: tuple[str, str] | None = None
    consecutive_tool_failures = 0
    tool_call_counts: dict[tuple[str, str], int] = {}
    seen_observations: dict[str, tuple[str, str]] = {}
    token_budget_warning_emitted = False

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

        current_model = synthesize_model if mode == "synthesize" else explore_model
        step = iterations + 1
        ctx.iterations = step
        await ctx.events.emit("thinking", {"step": step, "model": current_model, "mode": mode})

        compacted = _compact_history_if_needed(messages)
        if compacted:
            await ctx.events.emit(
                "status",
                {
                    "phase": "history_compacted",
                    "compacted_tool_results": compacted,
                    "estimated_tokens": _estimate_tokens(messages),
                },
            )

        try:
            resp = await asyncio.wait_for(
                litellm.acompletion(
                    model=current_model,
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
        except RateLimitError as exc:
            backoff = min(60.0, 5.0 * (2**rate_limit_retries)) + random.uniform(0, 5)
            rate_limit_retries += 1
            if rate_limit_retries > 3:
                error_msg = f"LLM rate-limited after retries: {exc}"
                break
            await ctx.events.emit(
                "status",
                {"phase": "rate_limited_backoff", "seconds": round(backoff, 2)},
            )
            await asyncio.sleep(backoff)
            continue
        except Exception as exc:  # noqa: BLE001
            error_msg = f"LLM call raised: {type(exc).__name__}: {exc}"
            break

        iterations = step
        rate_limit_retries = 0

        usage = getattr(resp, "usage", None)
        if usage is not None:
            try:
                ctx.tokens_used += int(getattr(usage, "total_tokens", 0) or 0)
            except (TypeError, ValueError):
                pass
        if (
            not token_budget_warning_emitted
            and ctx.tokens_used >= int(settings.MAX_TOKENS_PER_RUN * 0.8)
        ):
            token_budget_warning_emitted = True
            await ctx.events.emit(
                "status",
                {
                    "phase": "token_budget_warning",
                    "tokens_used": ctx.tokens_used,
                    "max_tokens_per_run": settings.MAX_TOKENS_PER_RUN,
                    "percent_used": 80,
                },
            )

        choice = resp.choices[0]
        msg = choice.message
        # Normalize to dict the way LiteLLM expects on the next turn.
        msg_dict: dict[str, Any] = {
            "role": "assistant",
            "content": getattr(msg, "content", None),
        }
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            tool_names = {tc.function.name for tc in tool_calls}
            if mode == "explore" and tool_names.intersection(synthesize_tools):
                mode = "synthesize"
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
            tool_call_key = (name, raw_args)
            tool_call_count = tool_call_counts.get(tool_call_key, 0) + 1
            tool_call_counts[tool_call_key] = tool_call_count
            if tool_call_count > settings.MAX_REPEATED_TOOL_CALLS:
                observation = _repeated_tool_call_observation()
            else:
                observation = await dispatch(ctx, name, raw_args)
            await ctx.events.emit(
                "tool_result",
                {"name": name, "result_preview": observation[:1500]},
            )

            if _is_tool_exception_observation(observation):
                failure_key = (name, raw_args)
                if failure_key == last_failure_key:
                    consecutive_tool_failures += 1
                else:
                    last_failure_key = failure_key
                    consecutive_tool_failures = 1

                if consecutive_tool_failures >= settings.MAX_CONSECUTIVE_TOOL_FAILURES:
                    error_msg = (
                        f"tool {name!r} failed {consecutive_tool_failures} times "
                        f"in a row: {observation[:200]}"
                    )
            else:
                last_failure_key = None
                consecutive_tool_failures = 0

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _dedupe_tool_observation(
                        observation=observation,
                        name=name,
                        raw_args=raw_args,
                        seen_observations=seen_observations,
                    ),
                }
            )
            if error_msg or ctx.run_state.is_done:
                break

        if error_msg:
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
