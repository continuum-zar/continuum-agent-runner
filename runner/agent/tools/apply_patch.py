"""apply_patch tool: apply a unified diff via `git apply`."""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING, Any

from runner.sandbox.shell import run_shell_capped

if TYPE_CHECKING:
    from runner.agent.context import RunContext


async def handle(ctx: "RunContext", args: dict[str, Any]) -> str:
    patch = args.get("patch")
    if not isinstance(patch, str) or not patch.strip():
        return "[runner] apply_patch: 'patch' is required"

    fd, tmp = tempfile.mkstemp(prefix="agent_patch_", suffix=".diff")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(patch if patch.endswith("\n") else patch + "\n")
        rc, stdout, stderr = await run_shell_capped(
            ["git", "apply", "--whitespace=nowarn", tmp],
            cwd=ctx.workspace.root,
            timeout_seconds=60,
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    if rc != 0:
        # Try a more permissive `git apply -3` fallback that uses 3-way merging.
        fd, tmp = tempfile.mkstemp(prefix="agent_patch_", suffix=".diff")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(patch if patch.endswith("\n") else patch + "\n")
            rc2, out2, err2 = await run_shell_capped(
                ["git", "apply", "-3", "--whitespace=nowarn", tmp],
                cwd=ctx.workspace.root,
                timeout_seconds=60,
            )
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if rc2 != 0:
            return (
                "[runner] git apply failed.\n"
                f"first attempt stderr:\n{stderr}\n"
                f"3-way attempt stderr:\n{err2}"
            )
        return f"[runner] patch applied via 3-way merge.\n{out2}"

    return f"[runner] patch applied.\n{stdout}".rstrip() or "[runner] patch applied."
