"""Sandboxed shell execution: allow-list, output cap, timeout."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import Sequence, Union

from runner.config import settings
from runner.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Allow-list
# ---------------------------------------------------------------------------
# We accept a single command (the leading argv[0]) from this set. ``bash -lc``
# is allowed so the agent can express compound commands, but the body of the
# bash invocation is then re-validated against ``_DENY_RE`` to block obvious
# foot-guns. This is a defense-in-depth layer; the primary isolation is the
# per-run container + ephemeral workspace.

_ALLOWED_CMDS: frozenset[str] = frozenset(
    {
        # General Unix
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "grep",
        "rg",
        "find",
        "echo",
        "true",
        "false",
        "pwd",
        "which",
        "stat",
        "file",
        "diff",
        "sort",
        "uniq",
        "sed",
        "awk",
        "tr",
        "tee",
        "cp",
        "mv",
        "mkdir",
        "rmdir",
        "rm",
        "touch",
        "ln",
        "basename",
        "dirname",
        "realpath",
        "env",
        "printenv",
        "date",
        "yes",
        "tar",
        "zip",
        "unzip",
        "gzip",
        "gunzip",
        "jq",
        "xargs",
        "less",
        "more",
        "tree",
        # Git
        "git",
        # Python
        "python",
        "python3",
        "pip",
        "pip3",
        "uv",
        "poetry",
        "pytest",
        "ruff",
        "mypy",
        "black",
        "flake8",
        # Node/JS
        "node",
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "tsc",
        "eslint",
        "prettier",
        "vitest",
        "jest",
        # Other languages
        "go",
        "cargo",
        "rustc",
        "make",
        "cmake",
        "java",
        "javac",
        "mvn",
        "gradle",
        # Shell wrappers
        "bash",
        "sh",
    }
)


_DENY_RE = re.compile(
    r"""
    (^|[^a-zA-Z])(
        sudo
      | su\s
      | curl
      | wget
      | nc\b
      | ncat
      | ssh\b
      | scp\b
      | sftp\b
      | rsync\b
      | telnet
      | ftp\b
      | docker
      | kubectl
      | dd\b
      | mkfs\.
      | shutdown
      | reboot
      | halt
      | iptables
      | ufw\b
      | systemctl
      | service\b
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

_RM_RF_ROOT_RE = re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*[rR][a-zA-Z]*[fF]?[a-zA-Z]*\s+/\s")


class ShellPolicyError(Exception):
    """Raised when a shell command violates the allow-list."""


def _strip_env(env_overrides: dict[str, str] | None = None) -> dict[str, str]:
    """
    Build a minimal environment for child processes — strip secrets that might
    leak into untrusted code paths and only keep the bits a normal toolchain
    needs (PATH, HOME, lang locales, etc.).
    """
    base = os.environ.copy()
    keep = {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TZ",
        "USER",
        "LOGNAME",
        "SHELL",
        "PYTHONUNBUFFERED",
        "NODE_ENV",
        "NPM_CONFIG_LOGLEVEL",
        "CI",
        "DEBIAN_FRONTEND",
        # Allow git's HTTP config & GitHub auth helper if set
        "GIT_HTTP_USER_AGENT",
        "GIT_TERMINAL_PROMPT",
    }
    pruned = {k: v for k, v in base.items() if k in keep}
    pruned.setdefault("GIT_TERMINAL_PROMPT", "0")
    pruned.setdefault("CI", "true")
    pruned.setdefault("PYTHONUNBUFFERED", "1")
    if env_overrides:
        pruned.update(env_overrides)
    return pruned


def validate_command(argv: Sequence[str]) -> None:
    """
    Raise ``ShellPolicyError`` if ``argv`` is outside the allow-list or contains
    obvious dangerous patterns. ``argv[0]`` must be a bare command (no slashes)
    so we can match it against the allow-list cleanly.
    """
    if not argv:
        raise ShellPolicyError("empty command")
    head = argv[0]
    base_head = os.path.basename(head)
    if base_head != head:
        raise ShellPolicyError(
            f"absolute or relative paths in command name not allowed: {head!r}"
        )
    if base_head not in _ALLOWED_CMDS:
        raise ShellPolicyError(f"command not in allow-list: {base_head!r}")

    joined = " ".join(shlex.quote(a) for a in argv)
    if _DENY_RE.search(joined):
        raise ShellPolicyError(f"command contains a denied pattern: {joined[:200]!r}")
    if _RM_RF_ROOT_RE.search(" " + joined + " "):
        raise ShellPolicyError("'rm -rf /' style command rejected")

    # Special-case ``bash -lc "..."`` and ``sh -c "..."`` — the body must also
    # pass the deny regex. The agent often needs piped commands.
    if base_head in ("bash", "sh") and len(argv) >= 2 and argv[1] in ("-c", "-lc"):
        body = argv[2] if len(argv) >= 3 else ""
        if _DENY_RE.search(body):
            raise ShellPolicyError("shell -c body contains a denied pattern")
        if _RM_RF_ROOT_RE.search(" " + body + " "):
            raise ShellPolicyError("shell -c body contains 'rm -rf /'")


async def run_shell_capped(
    argv: Union[Sequence[str], str],
    *,
    cwd: Path,
    timeout_seconds: int = 60,
    env_overrides: dict[str, str] | None = None,
    skip_validation: bool = False,
) -> tuple[int, str, str]:
    """
    Run a sandboxed command and return (exit_code, stdout, stderr) with each
    stream truncated to ``MAX_SHELL_OUTPUT_BYTES``.

    Pass ``skip_validation=True`` only for trusted internal calls (e.g. ``git
    clone``, ``git config`` from ``workspace.py``); agent-supplied commands
    must always go through ``validate_command``.
    """
    if isinstance(argv, str):
        argv_list = shlex.split(argv)
    else:
        argv_list = list(argv)

    if not skip_validation:
        validate_command(argv_list)

    timeout_seconds = min(int(timeout_seconds), settings.MAX_SHELL_TIMEOUT_SECONDS)

    proc = await asyncio.create_subprocess_exec(
        *argv_list,
        cwd=str(cwd),
        env=_strip_env(env_overrides),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        return (
            124,
            "",
            f"[runner] command timed out after {timeout_seconds}s",
        )

    cap = settings.MAX_SHELL_OUTPUT_BYTES
    out = _truncate(stdout_bytes, cap)
    err = _truncate(stderr_bytes, cap)
    return (proc.returncode if proc.returncode is not None else -1, out, err)


def _truncate(data: bytes, cap: int) -> str:
    if len(data) <= cap:
        return data.decode("utf-8", errors="replace")
    head = data[:cap].decode("utf-8", errors="replace")
    return head + f"\n[runner] output truncated to {cap} bytes ({len(data)} total)"
