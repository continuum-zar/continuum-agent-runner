#!/usr/bin/env bash
# Container entrypoint: log Codex in with LLM_API_KEY, then exec the runner.
#
# Codex 0.136.0 does not honor OPENAI_API_KEY from env at exec time; it expects
# a persisted login under $CODEX_HOME (default ~/.codex). We do that once on
# container start so every subsequent `codex exec` the runner spawns is already
# authenticated.

set -euo pipefail

if [ -z "${LLM_API_KEY:-}" ]; then
  echo "[entrypoint] LLM_API_KEY is not set — cannot authenticate Codex" >&2
  exit 1
fi

# Idempotent: re-login on every boot so a rotated key takes effect on restart.
printf '%s' "$LLM_API_KEY" | codex login --with-api-key >/dev/null
echo "[entrypoint] codex login ok ($(codex login status 2>&1 | tail -1))"

exec python -m runner.main
