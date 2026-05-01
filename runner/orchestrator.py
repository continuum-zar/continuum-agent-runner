"""End-to-end lifecycle for a single agent job."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Optional

import redis.asyncio as aioredis

from runner.agent.context import RunContext, RunState
from runner.agent.loop import run_agent
from runner.backend_client import BackendClient
from runner.config import settings
from runner.events import EventPublisher
from runner.github.client import clone_url_with_token
from runner.logger import get_logger
from runner.models import AgentJob, AgentRunResult
from runner.sandbox.workspace import Workspace, create_workspace

logger = get_logger(__name__)


def _agent_branch_for(job: AgentJob) -> str:
    suffix = secrets.token_hex(3)
    return f"continuum/agent/task-{job.task_id}-run-{suffix}"


async def _watch_for_cancel(
    redis_client: aioredis.Redis,
    run_id: str,
    state: RunState,
    stop_evt: asyncio.Event,
) -> None:
    """Listen for cancel messages on the run's control channel."""
    channel = settings.CONTROL_CHANNEL_TEMPLATE.format(run_id=run_id)
    pubsub = redis_client.pubsub()
    try:
        await pubsub.subscribe(channel)
        while not stop_evt.is_set():
            try:
                msg = await asyncio.wait_for(pubsub.get_message(ignore_subscribe_messages=True), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            if msg is None:
                continue
            try:
                data = msg.get("data")
                if isinstance(data, (bytes, bytearray)):
                    data = data.decode("utf-8", errors="replace")
                payload = json.loads(data) if isinstance(data, str) else data
            except Exception:  # noqa: BLE001
                payload = {}
            if isinstance(payload, dict) and payload.get("kind") == "cancel":
                state.cancel_requested = True
                logger.info("orchestrator.cancel_received", run_id=run_id)
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass


async def process_job(
    job: AgentJob,
    *,
    redis_client: aioredis.Redis,
    backend: BackendClient,
) -> None:
    """
    Drive a single agent run from start to cleanup. Errors at any phase are
    surfaced as run-level failures so the UI sees a clean terminal state.
    """
    publisher = EventPublisher(job.run_id, redis_client, backend)

    workspace: Optional[Workspace] = None
    cancel_task: Optional[asyncio.Task] = None
    cancel_stop = asyncio.Event()

    try:
        await backend.update_status(job.run_id, "running")
        await publisher.emit("status", {"phase": "started", "run_id": job.run_id})

        # ---- 1. Get a GitHub installation token --------------------------
        await publisher.emit("status", {"phase": "fetching_github_token"})
        try:
            token = await backend.fetch_installation_token(job.linked_repo)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"github_token_fetch_failed: {exc}") from exc

        # ---- 2. Clone + checkout ----------------------------------------
        agent_branch: Optional[str] = None
        if job.mode == "open_pr":
            agent_branch = _agent_branch_for(job)

        await publisher.emit(
            "status",
            {
                "phase": "cloning_repo",
                "repo": job.linked_repo,
                "branch": job.linked_branch,
                "agent_branch": agent_branch,
            },
        )

        try:
            workspace = await create_workspace(
                run_id=job.run_id,
                repo_full_name=job.linked_repo,
                clone_url_with_token=clone_url_with_token(job.linked_repo, token),
                branch=job.linked_branch,
                agent_branch=agent_branch,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"clone_failed: {exc}") from exc

        await publisher.emit(
            "status",
            {
                "phase": "workspace_ready",
                "active_branch": workspace.branch,
                "base_branch": workspace.base_branch,
            },
        )

        # ---- 3. Build context & run the agent ---------------------------
        ctx = RunContext(
            job=job,
            workspace=workspace,
            events=publisher,
            github_token=token,
        )

        cancel_task = asyncio.create_task(
            _watch_for_cancel(redis_client, job.run_id, ctx.run_state, cancel_stop)
        )

        result: AgentRunResult = await run_agent(ctx)

        await publisher.emit(
            "status",
            {
                "phase": "completed",
                "result_status": result.status,
                "iterations": result.iterations,
                "tokens_used": result.tokens_used,
            },
        )

        await backend.finalize(job.run_id, result)

    except Exception as exc:  # noqa: BLE001
        logger.exception("orchestrator.process_job_failed", run_id=job.run_id, error=str(exc))
        try:
            await publisher.emit("error", {"error": str(exc)[:1000]})
        except Exception:  # noqa: BLE001
            pass
        try:
            await backend.finalize(
                job.run_id,
                AgentRunResult(
                    status="failed",
                    error=str(exc)[:1000],
                ),
            )
        except Exception:  # noqa: BLE001
            pass
    finally:
        cancel_stop.set()
        if cancel_task is not None:
            cancel_task.cancel()
            try:
                await cancel_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if workspace is not None:
            await workspace.cleanup()
