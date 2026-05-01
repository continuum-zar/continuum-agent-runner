"""Entrypoint: subscribe to the agent jobs stream and dispatch to workers."""

from __future__ import annotations

import asyncio
import json
import signal
import socket
from typing import Any

import redis.asyncio as aioredis

from runner.backend_client import BackendClient
from runner.config import settings
from runner.logger import configure_logging, get_logger
from runner.models import AgentJob
from runner.orchestrator import process_job

configure_logging()
logger = get_logger(__name__)


async def _ensure_consumer_group(redis_client: aioredis.Redis) -> None:
    """Create the consumer group on the jobs stream (idempotent)."""
    try:
        await redis_client.xgroup_create(
            settings.JOB_STREAM, settings.JOB_CONSUMER_GROUP, id="0", mkstream=True
        )
        logger.info(
            "redis.consumer_group_created",
            stream=settings.JOB_STREAM,
            group=settings.JOB_CONSUMER_GROUP,
        )
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            return
        raise


async def _claim_stale(redis_client: aioredis.Redis, consumer: str) -> list[Any]:
    """Auto-claim entries from dead consumers so jobs don't get stuck."""
    try:
        out = await redis_client.xautoclaim(
            settings.JOB_STREAM,
            settings.JOB_CONSUMER_GROUP,
            consumer,
            min_idle_time=settings.JOB_STALE_IDLE_MS,
            start_id="0-0",
            count=10,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis.xautoclaim_failed", error=str(exc))
        return []
    # redis-py returns (next_cursor, entries, deleted_ids)
    if isinstance(out, (list, tuple)) and len(out) >= 2:
        return list(out[1] or [])
    return []


async def _decode_entry(entry: Any) -> AgentJob | None:
    """Decode an XREADGROUP entry into an AgentJob."""
    try:
        # entry is (id, {field: value})
        _eid, fields = entry
        if isinstance(fields, dict):
            payload = fields.get(b"payload") or fields.get("payload")
        else:
            payload = None
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", errors="replace")
        if not isinstance(payload, str):
            return None
        data = json.loads(payload)
        return AgentJob.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.exception("decode_entry_failed", error=str(exc))
        return None


async def _worker(
    worker_id: int,
    redis_client: aioredis.Redis,
    backend: BackendClient,
    stop_evt: asyncio.Event,
) -> None:
    consumer = f"{socket.gethostname()}-{worker_id}"
    logger.info("worker.started", worker_id=worker_id, consumer=consumer)

    while not stop_evt.is_set():
        # First try to pick up any stale, un-acked work.
        stale = await _claim_stale(redis_client, consumer)
        entries: list[Any] = list(stale)

        if not entries:
            try:
                # Block for up to 5s waiting for a fresh job. ``>`` means "new
                # entries delivered to this consumer group only".
                resp = await redis_client.xreadgroup(
                    groupname=settings.JOB_CONSUMER_GROUP,
                    consumername=consumer,
                    streams={settings.JOB_STREAM: ">"},
                    count=1,
                    block=5_000,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("redis.xreadgroup_failed", error=str(exc))
                await asyncio.sleep(1)
                continue
            if not resp:
                continue
            # resp: [(stream, [(id, fields), ...]), ...]
            for _stream, stream_entries in resp:
                entries.extend(stream_entries)

        for raw_entry in entries:
            if stop_evt.is_set():
                break
            entry_id = raw_entry[0]
            if isinstance(entry_id, (bytes, bytearray)):
                entry_id = entry_id.decode("utf-8", errors="replace")

            job = await _decode_entry(raw_entry)
            if job is None:
                # Acknowledge so we don't keep retrying garbage forever.
                try:
                    await redis_client.xack(
                        settings.JOB_STREAM, settings.JOB_CONSUMER_GROUP, entry_id
                    )
                except Exception:  # noqa: BLE001
                    pass
                continue

            logger.info(
                "worker.processing",
                worker_id=worker_id,
                run_id=job.run_id,
                task_id=job.task_id,
                mode=job.mode,
            )

            try:
                await process_job(job, redis_client=redis_client, backend=backend)
            except Exception as exc:  # noqa: BLE001
                logger.exception("worker.process_job_unhandled", error=str(exc))
            finally:
                try:
                    await redis_client.xack(
                        settings.JOB_STREAM, settings.JOB_CONSUMER_GROUP, entry_id
                    )
                except Exception:  # noqa: BLE001
                    pass

    logger.info("worker.stopped", worker_id=worker_id)


async def _main_async() -> None:
    if not settings.AGENT_RUNNER_HMAC_SECRET:
        logger.warning(
            "config.hmac_secret_missing",
            note="AGENT_RUNNER_HMAC_SECRET not set; backend callbacks will be rejected.",
        )
    if not settings.LLM_API_KEY:
        logger.warning(
            "config.llm_api_key_missing",
            note="LLM_API_KEY is empty; the agent will fail to call the model.",
        )

    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=False)
    backend = BackendClient()

    try:
        await redis_client.ping()
    except Exception as exc:  # noqa: BLE001
        logger.error("redis.ping_failed", error=str(exc))
        raise

    await _ensure_consumer_group(redis_client)

    stop_evt = asyncio.Event()

    def _request_stop(*_args: Any) -> None:
        logger.info("signal.shutdown_requested")
        stop_evt.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows / non-unix
            signal.signal(sig, _request_stop)

    workers = [
        asyncio.create_task(_worker(i, redis_client, backend, stop_evt))
        for i in range(max(1, settings.RUNNER_CONCURRENCY))
    ]

    logger.info(
        "runner.ready",
        concurrency=settings.RUNNER_CONCURRENCY,
        stream=settings.JOB_STREAM,
        group=settings.JOB_CONSUMER_GROUP,
    )

    try:
        await stop_evt.wait()
    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await backend.aclose()
        await redis_client.aclose()


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
