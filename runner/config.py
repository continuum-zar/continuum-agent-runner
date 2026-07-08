"""Runner configuration loaded from environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Connectivity
    REDIS_URL: str = "redis://localhost:6379/0"
    BACKEND_URL: str = "http://localhost:8001"
    AGENT_RUNNER_HMAC_SECRET: str = ""

    # LLM
    LLM_API_KEY: str = ""
    LLM_PROVIDER: str = "openai"
    LLM_MODEL: str = "gpt-5-codex"
    # OpenAI-compatible endpoint used for the light orchestration calls below
    # (Codex itself authenticates separately via `codex login`).
    LLM_BASE_URL: str = "https://api.openai.com/v1"

    # Orchestration: a large "orchestrator" model decomposes the task into an
    # execution plan and delegates light work to small, independent workers —
    # parallel repo "scouts" before the coding agent starts, and a verifier
    # after it finishes. Codex remains the single coding executor; disabling
    # this falls back to the plain single-Codex run.
    ORCHESTRATION_ENABLED: bool = True
    LLM_ORCHESTRATOR_MODEL: str = "gpt-4.1"
    LLM_WORKER_MODEL: str = "gpt-4.1-mini"
    MAX_SCOUT_WORKERS: int = 3
    WORKER_LLM_TIMEOUT_SECONDS: int = 60
    # Ceiling for the whole pre-flight phase (orchestrator plan + parallel scouts).
    SCOUT_PHASE_TIMEOUT_SECONDS: int = 300
    VERIFIER_ENABLED: bool = True

    # Codex CLI
    CODEX_BIN: str = "codex"
    # workspace-write uses bwrap, which fails on hosts that restrict unprivileged
    # user namespaces. The runner already isolates each job in its own dir, so
    # danger-full-access is the right default here.
    CODEX_SANDBOX: str = "danger-full-access"

    # GitHub App credentials (the runner mints installation tokens locally
    # to avoid round-tripping the API on every clone).
    GITHUB_APP_ID: str = ""
    GITHUB_APP_PRIVATE_KEY: str = ""

    # Worker tuning
    RUNNER_CONCURRENCY: int = 2
    # Must stay comfortably ABOVE MAX_WALL_CLOCK_SECONDS: a job's stream message
    # is only xack'd after it finishes, so any run still going past this idle
    # window gets reclaimed and re-executed by another worker. Kept ~10min above
    # the wall-clock ceiling below.
    JOB_STALE_IDLE_MS: int = 15_000_000  # ~4h10m
    WORKSPACE_ROOT: str = "/work"
    LOG_LEVEL: str = "INFO"

    # Hard guardrails
    # Wall-clock ceiling for a single run. Large tasks can legitimately take a
    # long time, so this is generous; it only exists to reap runs that are truly
    # stuck. Raise via env (MAX_WALL_CLOCK_SECONDS) if you need even longer, and
    # bump JOB_STALE_IDLE_MS to stay above it.
    MAX_WALL_CLOCK_SECONDS: int = 14_400  # 4h
    # Token budget you can afford per run. Tokens are only reported after each
    # turn completes, so the run is killed at (cap - headroom) to leave room for
    # one more turn's worth of tokens before it crosses the real ceiling.
    MAX_TOKENS_PER_RUN: int = 2_000_000
    TOKEN_BUDGET_HEADROOM: int = 200_000
    MAX_SHELL_OUTPUT_BYTES: int = 64_000
    MAX_SHELL_TIMEOUT_SECONDS: int = 600
    # asyncio StreamReader buffer for codex's `--json` stdout/stderr. Each event
    # is a single JSON line that embeds tool output (e.g. a full `npm run build`
    # log), so the asyncio default of 64 KiB per line overflows and raises
    # "Separator is not found, and chunk exceed the limit", aborting the run.
    # Size this to hold a large single event; lines still bigger than this are
    # skipped rather than fatal (see codex_runner).
    CODEX_STREAM_LIMIT_BYTES: int = 16_777_216  # 16 MiB

    # Stream / channel names (must match the API)
    JOB_STREAM: str = "continuum:agent:jobs"
    JOB_CONSUMER_GROUP: str = "agent-runners"
    EVENT_CHANNEL_TEMPLATE: str = "continuum:agent:run:{run_id}"
    CONTROL_CHANNEL_TEMPLATE: str = "continuum:agent:control:{run_id}"

    # Built-in commit author for direct-push and PR-mode commits.
    COMMIT_AUTHOR_NAME: str = Field(default="Continuum Agent")
    COMMIT_AUTHOR_EMAIL: str = Field(default="agent@continuumapp.co.za")


settings = Settings()
