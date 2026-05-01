"""Shared data shapes between the API and the runner."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


AgentRunMode = Literal["direct_push", "open_pr"]
AgentRunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class AgentJob(BaseModel):
    """Job payload as published by the API on the Redis stream."""

    run_id: str
    task_id: int
    project_id: int
    linked_repo: str  # "owner/name"
    linked_branch: str
    linked_branch_full_ref: Optional[str] = None
    mode: AgentRunMode
    instructions: Optional[str] = None
    initiating_user_id: Optional[int] = None
    # Bag of context the API has already gathered (task title/desc/checklists,
    # comments, RAG snippets, etc.). Free-form for forward-compat.
    context: dict[str, Any] = Field(default_factory=dict)


AgentEventKind = Literal[
    "status",
    "thinking",
    "tool_call",
    "tool_result",
    "shell_stdout",
    "commit",
    "final_message",
    "error",
    "cancelled",
]


class AgentEvent(BaseModel):
    """A single timeline entry emitted during an agent run."""

    run_id: str
    seq: int = 0  # filled in by the publisher; monotonically increasing per run
    kind: AgentEventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def model_dump_for_wire(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        # Pydantic emits `datetime` as iso, which is what we want.
        return d


class AgentRunResult(BaseModel):
    """Final outcome of a run, posted to the API's /finalize endpoint."""

    status: AgentRunStatus
    summary: Optional[str] = None
    error: Optional[str] = None
    agent_branch: Optional[str] = None
    commit_sha: Optional[str] = None
    pr_url: Optional[str] = None
    iterations: int = 0
    tokens_used: int = 0
    cost_usd: Optional[float] = None
