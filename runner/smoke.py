"""Smoke test for the Codex runner.

Spawns `codex exec` against a throwaway local git repo, prints every event the
mapper produces, and SKIPS the final git push so no remote is needed.

Use it to validate:
  - that the Codex CLI is installed and authenticated,
  - that the flag set in `codex_runner._spawn_codex` is accepted by the
    installed Codex version,
  - that the JSON event `type` strings we match in `_handle_codex_event` are
    the ones the installed Codex actually emits (anything unknown will be
    logged as `codex.unknown_event_type`).

Usage:
    OPENAI_API_KEY=sk-... python -m runner.smoke \\
        "Add a hello() function to main.py that prints 'hi from codex'."

Exit status is the run's success/failure; the workspace is wiped on exit.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from runner.agent import codex_runner
from runner.agent.context import RunContext
from runner.models import AgentJob
from runner.sandbox.workspace import Workspace


class PrintPublisher:
    """Stand-in for EventPublisher — just prints to stdout."""

    def __init__(self) -> None:
        self.seq = 0

    async def emit(self, kind, payload=None):  # type: ignore[override]
        self.seq += 1
        preview = payload or {}
        # Keep one event per line so you can pipe through grep.
        print(f"[{self.seq:03d}] {kind}: {preview}", flush=True)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["git", "init", "-q", "-b", "main"], cwd=root)
    subprocess.check_call(["git", "config", "user.email", "smoke@example.com"], cwd=root)
    subprocess.check_call(["git", "config", "user.name", "smoke"], cwd=root)
    (root / "main.py").write_text("print('hi')\n")
    (root / "README.md").write_text("# smoke repo\n")
    subprocess.check_call(["git", "add", "."], cwd=root)
    subprocess.check_call(["git", "commit", "-qm", "initial"], cwd=root)


async def main() -> int:
    if len(sys.argv) < 2:
        print(
            "usage: python -m runner.smoke '<task instruction>'",
            file=sys.stderr,
        )
        return 2
    instruction = sys.argv[1]

    tmp = Path(tempfile.mkdtemp(prefix="codex-smoke-"))
    repo = tmp / "repo"
    try:
        _init_repo(repo)

        workspace = Workspace(
            run_id="smoke",
            root=repo.resolve(),
            repo_full_name="smoke/local",
            branch="main",
            base_branch="main",
        )
        job = AgentJob(
            run_id="smoke",
            task_id=0,
            project_id=0,
            linked_repo="smoke/local",
            linked_branch="main",
            mode="direct_push",
            instructions=instruction,
            context={"task_title": "Smoke test"},
        )
        ctx = RunContext(
            job=job,
            workspace=workspace,
            events=PrintPublisher(),  # type: ignore[arg-type]
            github_token="not-a-real-token",
        )

        # Stub out the push/PR step so we don't need a remote.
        async def _noop_commit(_ctx, summary):  # type: ignore[no-untyped-def]
            _ctx.run_state.summary = summary or "smoke run complete (push skipped)"

        codex_runner._commit_push_pr = _noop_commit  # type: ignore[assignment]

        result = await codex_runner.run_agent(ctx)
        print("\n=== RESULT ===", flush=True)
        print(result.model_dump_json(indent=2))

        # Also show what Codex actually changed in the workspace.
        diff = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=False,
        )
        if diff.stdout.strip():
            print("\n=== WORKSPACE DIFF ===")
            print(diff.stdout)
        else:
            print("\n(no changes in working tree)")

        return 0 if result.status == "succeeded" else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
