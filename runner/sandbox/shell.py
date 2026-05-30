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
# foot-guns.
#
# IMPORTANT: this list is a *speed-bump*, not a sandbox. The runner ships as a
# single Docker container shared across concurrent runs (no gVisor / seccomp
# per run), and we deliberately allow language runtimes (``python``, ``node``,
# ``pip``, ``npm`` …) so the agent can install deps and run tests — which
# means an adversarial agent can execute arbitrary code regardless of what
# this allow-list contains. The list exists to (a) reject obvious LLM
# goof-ups before they hit the shell, and (b) give a clear "policy violation"
# error the model can react to.
#
# See ``README.md`` → "Sandbox & security model" for the threat model and the
# rationale behind each retained command.

_ALLOWED_CMDS: frozenset[str] = frozenset(
    {
        # General Unix — read/inspect
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
        # File mutation (workspace-relative paths only — enforced by
        # ``_ABSOLUTE_PATH_GUARD_CMDS`` below)
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
        # Env inspection — only as a prefix to a child command (``env FOO=1
        # cmd``); the deny regex blocks a bare ``env`` / ``printenv`` that
        # would dump the runner's secrets.
        "env",
        "printenv",
        "date",
        # Archive tools — needed for some build steps. Note: ``curl``/``wget``
        # remain denied so these can only repack workspace contents, not
        # exfiltrate over the network directly.
        "tar",
        "zip",
        "unzip",
        "gzip",
        "gunzip",
        "jq",
        "xargs",
        "tree",
        # Dropped from the allow-list (see git history): ``yes`` (output
        # flooder, no agent use-case), ``tee`` (write_file tool covers writes
        # and ``tee /etc/...`` is the classic privilege-escalation vector),
        # ``less`` / ``more`` (interactive pagers hang on non-TTY pipes).
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


# Patterns that should NEVER appear anywhere in the command line, regardless
# of which allow-listed program is invoking them. Three rough buckets:
#   1. Network egress / remote shells (curl, wget, ssh, …) — the agent has no
#      need to reach the internet via shell; ``git`` and ``pip`` go through
#      their own configured endpoints.
#   2. Host/privilege mutation (sudo, mount, chown, useradd, systemctl, …) —
#      the runner runs as a non-root user inside its container, but these
#      have no agent use-case and indicate something is very wrong if they
#      show up.
#   3. Reverse-shell / secret-exfil signatures (``/dev/tcp/``, ``bash -i``,
#      ``mkfifo``, bare ``env``/``printenv``, ``/proc/.../environ``, …) —
#      these are the patterns an attacker-controlled prompt would use to dump
#      the container's secret env vars or open an outbound shell.
_DENY_RE = re.compile(
    r"""
    (?:^|[^a-zA-Z0-9._/-])(
        # --- network egress / remote shells ---
        sudo
      | su\s
      | curl
      | wget
      | nc\b
      | ncat
      | netcat
      | ssh\b
      | scp\b
      | sftp\b
      | rsync\b
      | telnet
      | ftp\b
      | socat
      | docker
      | kubectl
      | helm\b
      # --- host / privilege mutation ---
      | dd\b
      | mkfs\.
      | shutdown
      | reboot
      | halt
      | poweroff
      | iptables
      | nft\b
      | ufw\b
      | systemctl
      # SysV "service NAME ACTION" — narrowed so the word "service" in a
      # commit message or grep pattern doesn't false-positive.
      | service\s+[A-Za-z0-9_-]+\s+(?:start|stop|restart|reload|status|enable|disable)\b
      | mount\b
      | umount\b
      | chown\b
      | useradd
      | usermod
      | userdel
      | groupadd
      | groupmod
      | groupdel
      | passwd\b
      | crontab\b
      # setuid / setgid bit on chmod (numeric or symbolic)
      | chmod\s+(?:[0-7]?[2-7]\d{3}|[ugoa]*[+-]s)
      # --- shell-internal escapes ---
      # ``eval`` / ``exec`` of arbitrary strings, sourcing arbitrary files,
      # interactive bash spawns
      | eval\s
      | exec\s+(?:bash|sh|python|node)
      | source\s
      | \.\s+/
      | bash\s+-i\b
      | sh\s+-i\b
      # --- reverse shell / pipe-fd tricks ---
      | /dev/tcp/
      | /dev/udp/
      | mkfifo\b
      # --- secret-bearing files / env dumps ---
      | /proc/[^\s]*/environ
      | /etc/shadow\b
      | /etc/sudoers
      | (?:^|/)\.ssh/
      | (?:^|/)\.aws/credentials
      | (?:^|/)\.netrc\b
      | (?:^|/)\.git-credentials\b
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Bare ``env`` / ``printenv`` (no args, or only piped/redirected to another
# command) dumps the runner's process env, which contains the LLM API key,
# the HMAC secret, the GitHub App private key, and clone tokens. Block that
# specific shape while still allowing ``env FOO=bar cmd …`` (env-as-prefix)
# and ``printenv FOO`` (single-var lookup).
_ENV_DUMP_RE = re.compile(
    r"""
    (?:^|[;&|]\s*)            # start of line or after a pipe/sep
    (?:env|printenv)
    \s*
    (?:$|[|>;&])              # nothing after, or only redirection
    """,
    re.VERBOSE,
)

# Reject ``rm -rf <absolute-path>`` and ``rm -rf ~`` / ``rm -rf $HOME``. The
# previous version only caught the literal ``rm -rf /`` with surrounding
# whitespace, so ``rm -rf /etc`` and ``rm -rf /*`` slipped through.
_RM_RF_ABSOLUTE_RE = re.compile(
    r"""
    \brm\b
    [^|;&]*                    # any flags/args up to the next separator
    \s-[a-zA-Z]*[rR][a-zA-Z]*  # the -r/-R flag (alongside -f, -i, …)
    [^|;&]*?
    \s
    (?:                        # the target path:
        /                      #   any absolute path
      | ~                      #   $HOME shorthand
      | \$HOME\b
      | \$\{HOME\}
    )
    """,
    re.VERBOSE,
)

# argv[0] commands whose path arguments must stay inside the workspace. We
# enforce this by rejecting any argument that *starts* with ``/`` or ``~``
# (absolute path / home-relative). Workspace-relative paths are unaffected,
# and ``--flag=value`` style args are skipped.
_ABSOLUTE_PATH_GUARD_CMDS: frozenset[str] = frozenset(
    {"rm", "rmdir", "cp", "mv", "ln", "mkdir", "touch", "chmod"}
)


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


def _check_string(text: str, *, where: str) -> None:
    """Apply every deny pattern to a single command-line string."""
    if _DENY_RE.search(text):
        raise ShellPolicyError(f"{where} contains a denied pattern")
    if _ENV_DUMP_RE.search(text):
        raise ShellPolicyError(
            f"{where} would dump the runner's environment (secrets) — "
            "use ``printenv FOO`` for a specific variable instead"
        )
    if _RM_RF_ABSOLUTE_RE.search(" " + text + " "):
        raise ShellPolicyError(
            f"{where} contains 'rm -rf' against an absolute path or $HOME"
        )


def _check_absolute_path_args(cmd: str, args: Sequence[str]) -> None:
    """For destructive file commands, reject absolute / home-relative paths.

    The agent's cwd is the workspace root; absolute paths can otherwise reach
    the container's filesystem (other runs' workspaces, /etc, /root, …). Flag
    values like ``--target=/foo`` are ignored — the agent passes paths as
    positional args in practice.
    """
    if cmd not in _ABSOLUTE_PATH_GUARD_CMDS:
        return
    for arg in args:
        if arg.startswith("-"):
            continue
        if arg.startswith("/") or arg.startswith("~"):
            raise ShellPolicyError(
                f"{cmd!r} argument {arg!r} is outside the workspace; "
                "use a workspace-relative path"
            )


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

    _check_absolute_path_args(base_head, argv[1:])

    joined = " ".join(shlex.quote(a) for a in argv)
    _check_string(joined, where="command")

    # Special-case ``bash -lc "..."`` and ``sh -c "..."`` — the body must also
    # pass every deny check. The agent often needs piped commands, so we have
    # to re-validate the inner string with the same rules.
    if base_head in ("bash", "sh") and len(argv) >= 2 and argv[1] in ("-c", "-lc"):
        body = argv[2] if len(argv) >= 3 else ""
        _check_string(body, where="shell -c body")


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
