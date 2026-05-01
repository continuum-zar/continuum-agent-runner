"""HMAC-signed HTTP client for talking back to the Continuum API."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Optional

import httpx

from runner.config import settings
from runner.logger import get_logger
from runner.models import AgentRunResult

logger = get_logger(__name__)


def _sign(body: bytes, ts: str) -> str:
    secret = settings.AGENT_RUNNER_HMAC_SECRET.encode("utf-8")
    msg = ts.encode("utf-8") + b"." + body
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


class BackendClient:
    """
    Posts run-lifecycle data back to the Continuum API. Every request includes
    `X-Agent-Runner-Timestamp` and `X-Agent-Runner-Signature` headers; the API
    side recomputes the HMAC and rejects mismatches.
    """

    def __init__(self) -> None:
        self._base = settings.BACKEND_URL.rstrip("/")
        self._client = httpx.AsyncClient(timeout=20.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ts = str(int(time.time()))
        sig = _sign(body, ts)
        url = f"{self._base}{path}"
        return await self._client.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Agent-Runner-Timestamp": ts,
                "X-Agent-Runner-Signature": sig,
            },
        )

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> httpx.Response:
        # GET requests sign an empty body; the timestamp + path is enough to bind
        # the request to a window.
        body = b""
        ts = str(int(time.time()))
        sig = _sign(body, ts)
        url = f"{self._base}{path}"
        return await self._client.get(
            url,
            params=params,
            headers={
                "X-Agent-Runner-Timestamp": ts,
                "X-Agent-Runner-Signature": sig,
            },
        )

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def post_event(self, run_id: str, event: dict[str, Any]) -> None:
        path = f"/api/v1/internal/agent/runs/{run_id}/events"
        r = await self._post(path, {"event": event})
        if r.status_code >= 400:
            logger.warning("backend.post_event_non_2xx", status=r.status_code, body=r.text[:300])

    async def update_status(self, run_id: str, status: str, error: Optional[str] = None) -> None:
        path = f"/api/v1/internal/agent/runs/{run_id}/status"
        payload: dict[str, Any] = {"status": status}
        if error:
            payload["error"] = error
        r = await self._post(path, payload)
        if r.status_code >= 400:
            logger.warning("backend.update_status_non_2xx", status=r.status_code, body=r.text[:300])

    async def finalize(self, run_id: str, result: AgentRunResult) -> None:
        path = f"/api/v1/internal/agent/runs/{run_id}/finalize"
        r = await self._post(path, result.model_dump(mode="json", exclude_none=True))
        if r.status_code >= 400:
            logger.warning("backend.finalize_non_2xx", status=r.status_code, body=r.text[:300])

    async def fetch_installation_token(self, repo_full_name: str) -> str:
        """
        Ask the backend to mint a GitHub App installation access token for the
        repo. The backend already owns the App credentials, so this is the
        most reliable path; we only fall back to local minting if the GitHub
        App env vars are also set on the runner.
        """
        path = "/api/v1/internal/agent/github/installation-token"
        r = await self._get(path, params={"repo": repo_full_name})
        if r.status_code >= 400:
            raise RuntimeError(
                f"installation_token_fetch_failed: {r.status_code} {r.text[:200]}"
            )
        data = r.json()
        token = data.get("token")
        if not token:
            raise RuntimeError("installation_token_missing_in_response")
        return token
