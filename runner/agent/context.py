"""Per-run mutable state shared across tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from runner.events import EventPublisher
from runner.models import AgentJob
from runner.sandbox.workspace import Workspace


@dataclass
class RunState:
    is_done: bool = False
    summary: Optional[str] = None
    last_commit_sha: Optional[str] = None
    pr_url: Optional[str] = None
    first_push: bool = True
    cancel_requested: bool = False


@dataclass
class RunContext:
    job: AgentJob
    workspace: Workspace
    events: EventPublisher
    github_token: str
    run_state: RunState = field(default_factory=RunState)
    iterations: int = 0
    tokens_used: int = 0
