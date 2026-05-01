"""done tool: marks the run as finished."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from runner.agent.context import RunContext


async def handle(ctx: "RunContext", args: dict[str, Any]) -> str:
    summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = "Run completed."
    ctx.run_state.is_done = True
    ctx.run_state.summary = summary.strip()
    return "[runner] done. summary recorded."
