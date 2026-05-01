"""Per-run filesystem workspace (clone, cleanup, path safety)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from runner.config import settings
from runner.logger import get_logger
from runner.sandbox.shell import run_shell_capped

logger = get_logger(__name__)


class WorkspacePathError(Exception):
    """Raised when the agent tries to read/write outside the workspace."""


@dataclass
class Workspace:
    """A per-run working directory containing a single git checkout."""

    run_id: str
    root: Path  # absolute path; everything the agent does happens under this
    repo_full_name: str
    branch: str  # the active branch (linked branch in direct mode, agent
    # branch in PR mode)
    base_branch: str  # always the linked branch (PR target / push target)

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def resolve(self, rel_path: str) -> Path:
        """
        Resolve ``rel_path`` (which the agent supplies) to an absolute path
        that is guaranteed to live inside the workspace root.
        """
        if not isinstance(rel_path, str):
            raise WorkspacePathError("path must be a string")
        if rel_path in ("", "."):
            return self.root
        candidate = (self.root / rel_path).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as exc:
            raise WorkspacePathError(
                f"path '{rel_path}' escapes the workspace root"
            ) from exc
        return candidate

    def relative(self, abs_path: Path) -> str:
        return str(abs_path.relative_to(self.root.resolve()))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cleanup(self) -> None:
        try:
            await _rmtree_async(self.root)
        except Exception as exc:  # noqa: BLE001
            logger.warning("workspace.cleanup_failed", run_id=self.run_id, error=str(exc))


async def _rmtree_async(path: Path) -> None:
    import asyncio

    def _rm() -> None:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    await asyncio.to_thread(_rm)


async def create_workspace(
    *,
    run_id: str,
    repo_full_name: str,
    clone_url_with_token: str,
    branch: str,
    agent_branch: Optional[str] = None,
) -> Workspace:
    """
    Clone ``repo_full_name`` into ``WORKSPACE_ROOT/<run_id>`` on ``branch``.
    If ``agent_branch`` is supplied (PR mode), create it from ``branch`` and
    check it out so subsequent commits land there.
    """
    root_dir = Path(settings.WORKSPACE_ROOT) / run_id
    root_dir.parent.mkdir(parents=True, exist_ok=True)
    if root_dir.exists():
        await _rmtree_async(root_dir)

    # Some repos use very deep histories; --depth=50 is a reasonable balance
    # between speed and giving the agent some context (so `git log` is useful).
    rc, stdout, stderr = await run_shell_capped(
        ["git", "clone", "--depth", "50", "--branch", branch, clone_url_with_token, str(root_dir)],
        cwd=Path(os.getcwd()),
        timeout_seconds=300,
    )
    if rc != 0:
        # Don't echo the clone URL (contains token) — strip it from logs.
        safe_stderr = stderr.replace(clone_url_with_token, "<clone-url>")
        raise RuntimeError(f"git_clone_failed: rc={rc} stderr={safe_stderr[:500]}")

    # Configure committer identity for any commits the agent makes.
    for k, v in (
        ("user.name", settings.COMMIT_AUTHOR_NAME),
        ("user.email", settings.COMMIT_AUTHOR_EMAIL),
        # Avoid pager / color escape codes in captured output.
        ("color.ui", "false"),
        ("core.pager", "cat"),
    ):
        await run_shell_capped(
            ["git", "config", k, v],
            cwd=root_dir,
            timeout_seconds=15,
        )

    active_branch = branch
    if agent_branch:
        rc, _, stderr = await run_shell_capped(
            ["git", "checkout", "-b", agent_branch],
            cwd=root_dir,
            timeout_seconds=30,
        )
        if rc != 0:
            raise RuntimeError(f"git_checkout_agent_branch_failed: {stderr[:300]}")
        active_branch = agent_branch

    return Workspace(
        run_id=run_id,
        root=root_dir.resolve(),
        repo_full_name=repo_full_name,
        branch=active_branch,
        base_branch=branch,
    )
