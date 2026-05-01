"""Publish agent events to Redis pub/sub and persist them via the backend."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import redis.asyncio as aioredis

from runner.backend_client import BackendClient
from runner.config import settings
from runner.logger import get_logger
from runner.models import AgentEvent, AgentEventKind

logger = get_logger(__name__)


class EventPublisher:
    """
    Fan-out for AgentEvent records:
      1. PUBLISH on the per-run Redis channel for live SSE subscribers.
      2. POST to the backend so the timeline is persisted (replay after refresh).

    Persisting via the backend is best-effort; if the backend is briefly down we
    log and continue so a transient blip doesn't kill the whole run.
    """

    def __init__(
        self,
        run_id: str,
        redis_client: aioredis.Redis,
        backend: BackendClient,
    ) -> None:
        self.run_id = run_id
        self._redis = redis_client
        self._backend = backend
        self._seq = 0
        self._lock = asyncio.Lock()

    async def emit(self, kind: AgentEventKind, payload: Optional[dict[str, Any]] = None) -> AgentEvent:
        async with self._lock:
            self._seq += 1
            event = AgentEvent(
                run_id=self.run_id,
                seq=self._seq,
                kind=kind,
                payload=payload or {},
            )
        wire = event.model_dump_for_wire()
        channel = settings.EVENT_CHANNEL_TEMPLATE.format(run_id=self.run_id)
        try:
            await self._redis.publish(channel, json.dumps(wire))
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis.publish_failed", run_id=self.run_id, kind=kind, error=str(exc))
        try:
            await self._backend.post_event(self.run_id, wire)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "backend.persist_event_failed",
                run_id=self.run_id,
                kind=kind,
                error=str(exc),
            )
        return event

    @property
    def seq(self) -> int:
        return self._seq
