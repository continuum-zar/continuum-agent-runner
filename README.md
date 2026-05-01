# Continuum Agent Runner

Background worker for the Continuum agentic task completor. Subscribes to a Redis
stream of pending agent runs published by the Continuum API, then for each job:

1. Fetches a GitHub App installation token (either by minting locally or via the
   API's signed `/internal/agent/github/installation-token` endpoint).
2. Clones the linked repository into a per-run workspace under `/work/<run_id>`.
3. Checks out the linked branch (or creates `continuum/agent/task-<id>-<run>` for
   PR mode).
4. Runs a homegrown LiteLLM tool-calling agent loop (read/write/list/patch/run_shell/git).
5. Pushes the final commit and (in PR mode) opens a PR back into the linked branch.
6. Streams `AgentEvent` messages to Redis Pub/Sub channel
   `continuum:agent:run:<run_id>`, which the API forwards to the browser via SSE.
7. Persists every event to the API via `/internal/agent/runs/<run_id>/events` so
   the Build run timeline is replayable.
8. Cleans up the workspace.

## Why a separate service

The Continuum API is a single uvicorn web container; running arbitrary user code
inside it would block requests, leak filesystem state across users, and break the
deploy boundary. The runner gets its own Dockerfile (with `git` + `nodejs`), its
own scaling knobs, and its own blast radius.

## Local dev

```bash
cp .env.example .env   # then fill REDIS_URL, BACKEND_URL, AGENT_RUNNER_HMAC_SECRET, LLM_*
pip install -r requirements.txt
python -m runner.main
```

## Railway

Deploy as a separate Railway service in the Continuum project. The Dockerfile in
this repo is what Railway builds; the `railway.toml` declares the start command
and skips the health check (this is a worker, not a web process).

Required env vars (see `.env.example`):

| var | purpose |
|-----|---------|
| `REDIS_URL` | Job stream + per-run pub/sub channel (must match the API service) |
| `BACKEND_URL` | Continuum API public URL, e.g. `https://api.continuumapp.co.za` |
| `AGENT_RUNNER_HMAC_SECRET` | Shared secret for HMAC-signed callbacks to the API |
| `LLM_API_KEY`, `LLM_PROVIDER`, `LLM_MODEL` | Same values as the API uses |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` | App credentials so the runner can mint installation tokens locally |
| `RUNNER_CONCURRENCY` | Worker tasks per process (default 2) |
| `WORKSPACE_ROOT` | Directory for per-run workspaces (default `/work`) |

## Tools the agent has

| tool | purpose |
|------|---------|
| `list_dir` | List files in a directory inside the workspace |
| `read_file` | Read a file (capped at 200 KB) |
| `write_file` | Write or overwrite a file |
| `apply_patch` | Apply a unified diff |
| `run_shell` | Run an allow-listed shell command |
| `git_status` | `git status --porcelain` |
| `git_diff` | `git diff` (working tree or staged) |
| `commit_and_push` | `git add` + `git commit` + `git push` (and create PR if mode=open_pr) |
| `done` | Finish the run with a final summary |

All tools operate inside the per-run workspace; absolute paths or `..` traversal
out of the workspace are rejected.
