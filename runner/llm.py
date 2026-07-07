"""Minimal OpenAI-compatible chat client for orchestration calls.

The heavy coding work is done by the Codex CLI subprocess; this client exists
for the light "manager" calls around it — task decomposition (orchestrator),
parallel repo scouts, and post-run verification — which use small models,
short prompts, and strict JSON outputs. Kept dependency-free beyond httpx so
the runner stays slim.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from runner.config import settings
from runner.logger import get_logger

logger = get_logger(__name__)


class LLMError(RuntimeError):
    """Raised when a chat completion fails or returns unusable output."""


async def chat(
    *,
    model: str,
    system: str,
    user: str,
    json_mode: bool = False,
    temperature: float = 0.2,
    max_tokens: int = 1200,
    timeout_seconds: Optional[int] = None,
) -> tuple[str, int]:
    """One chat completion. Returns (content, total_tokens)."""
    if not settings.LLM_API_KEY:
        raise LLMError("LLM_API_KEY is not configured")

    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    timeout = timeout_seconds or settings.WORKER_LLM_TIMEOUT_SECONDS
    url = settings.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
            json=body,
        )
    if resp.status_code != 200:
        raise LLMError(f"chat completion failed: HTTP {resp.status_code} {resp.text[:300]}")

    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"malformed completion response: {exc}") from exc

    usage = data.get("usage") or {}
    tokens = int(usage.get("total_tokens") or 0)
    return content, tokens


async def chat_json(
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.1,
    max_tokens: int = 1200,
    timeout_seconds: Optional[int] = None,
) -> tuple[dict[str, Any], int]:
    """Chat completion that must return a JSON object. Returns (obj, tokens)."""
    content, tokens = await chat(
        model=model,
        system=system,
        user=user,
        json_mode=True,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
    )
    try:
        obj = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError(f"worker returned invalid JSON: {content[:200]}") from exc
    if not isinstance(obj, dict):
        raise LLMError("worker returned JSON that is not an object")
    return obj, tokens
