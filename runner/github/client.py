"""Thin GitHub REST helper: PR creation + repo URL helpers."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

import httpx

GITHUB_API_BASE = "https://api.github.com"


def clone_url_with_token(repo_full_name: str, token: str) -> str:
    """
    Build an HTTPS clone URL with an embedded installation token.

    GitHub treats ``https://x-access-token:<token>@github.com/<owner>/<name>.git``
    as the canonical pattern for App installation tokens. The token is short
    lived (≤ 1h) so embedding it in the remote URL is acceptable for the
    duration of a single run.
    """
    safe_token = quote(token, safe="")
    return f"https://x-access-token:{safe_token}@github.com/{repo_full_name}.git"


async def create_pull_request(
    *,
    token: str,
    repo_full_name: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
) -> dict:
    """
    Open a PR from ``head_branch`` -> ``base_branch`` on ``repo_full_name``.
    Returns the GitHub API response (so the caller can grab ``html_url``).
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "title": title,
        "head": head_branch,
        "base": base_branch,
        "body": body,
        "maintainer_can_modify": True,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, headers=headers, json=payload)
    if r.status_code >= 400:
        raise RuntimeError(f"github_create_pr_failed: {r.status_code} {r.text[:300]}")
    return r.json()


async def get_repo_default_branch(*, token: str, repo_full_name: str) -> Optional[str]:
    """Fetch the repo's default branch (used as a fallback if base_branch is empty)."""
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers=headers)
    if r.status_code >= 400:
        return None
    return r.json().get("default_branch")
