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
    LLM_MODEL: str = "gpt-4.1"

    # GitHub App credentials (the runner mints installation tokens locally
    # to avoid round-tripping the API on every clone).
    GITHUB_APP_ID: str = ""
    GITHUB_APP_PRIVATE_KEY: str = ""

    # Worker tuning
    RUNNER_CONCURRENCY: int = 2
    WORKSPACE_ROOT: str = "/work"
    LOG_LEVEL: str = "INFO"

    # Hard guardrails
    MAX_ITERATIONS: int = 30
    MAX_WALL_CLOCK_SECONDS: int = 900
    MAX_TOKENS_PER_RUN: int = 400_000
    MAX_FILE_BYTES: int = 200_000
    MAX_SHELL_OUTPUT_BYTES: int = 64_000
    MAX_SHELL_TIMEOUT_SECONDS: int = 600

    # Stream / channel names (must match the API)
    JOB_STREAM: str = "continuum:agent:jobs"
    JOB_CONSUMER_GROUP: str = "agent-runners"
    EVENT_CHANNEL_TEMPLATE: str = "continuum:agent:run:{run_id}"
    CONTROL_CHANNEL_TEMPLATE: str = "continuum:agent:control:{run_id}"

    # Built-in commit author for direct-push and PR-mode commits.
    COMMIT_AUTHOR_NAME: str = Field(default="Continuum Agent")
    COMMIT_AUTHOR_EMAIL: str = Field(default="agent@continuumapp.co.za")


settings = Settings()
