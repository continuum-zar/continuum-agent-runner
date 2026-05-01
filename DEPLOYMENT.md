# Deploying the Agentic Task Completor on Railway

This doc walks through provisioning the new `continuum-agent-runner` service
and wiring it into the existing Continuum API + frontend on Railway.

## 1. Provision Redis (one plugin shared by both services)

The agent runner and the API talk over Redis (one stream + a pub/sub channel
per run), so both services need the **same** Redis instance.

1. In your Railway project, click **+ New** → **Database** → **Add Redis**.
2. Wait for it to provision. Railway exposes the connection string as the
   `REDIS_URL` environment variable on the plugin.
3. Reference that variable from the API service and from the agent-runner
   service:

   ```
   REDIS_URL=${{Redis.REDIS_URL}}
   ```

   (Use Railway's variable reference syntax; do NOT paste the raw string.)

The API only previously used Redis for optional shared rate limiting, so
existing deploys without Redis are fine — you just *must* provision it now
for the Build feature.

## 2. Generate the shared HMAC secret

The agent-runner posts back to the API with HMAC-signed requests. Generate a
strong secret locally:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Set the same value in **both** services as `AGENT_RUNNER_HMAC_SECRET`.

## 3. Create the new `continuum-agent-runner` Railway service

1. Push this repo (`continuum-agent-runner`) to GitHub.
2. In your Railway project: **+ New** → **GitHub Repo** → pick the new repo.
3. Railway detects the `Dockerfile` and `railway.toml` and builds.
4. On the service, set environment variables:

   | Variable | Value |
   |----------|-------|
   | `REDIS_URL` | `${{Redis.REDIS_URL}}` (or the same value the API uses) |
   | `BACKEND_URL` | Public URL of the Continuum API service (no trailing slash) |
   | `AGENT_RUNNER_HMAC_SECRET` | Same secret as on the API |
   | `LLM_API_KEY` | Same as the API (e.g. OpenAI key) |
   | `LLM_PROVIDER` | Same as the API (e.g. `openai`) |
   | `LLM_MODEL` | Same as the API (e.g. `gpt-4.1`) |
   | `GITHUB_APP_ID` | Same as the API (so the runner can mint installation tokens locally; if unset the runner falls back to fetching tokens from the API instead) |
   | `GITHUB_APP_PRIVATE_KEY` | Same as the API (paste the full PEM) |
   | `RUNNER_CONCURRENCY` | `2` (raise once you've validated stability) |
   | `WORKSPACE_ROOT` | `/work` (the Dockerfile creates this) |
   | `LOG_LEVEL` | `INFO` |

5. The `railway.toml` already disables the HTTP health check (this is a worker,
   not a web process). No public domain is required.

## 4. Add the new env vars to the existing Continuum API service

On the existing `continuum-backend` service add:

| Variable | Value |
|----------|-------|
| `REDIS_URL` | `${{Redis.REDIS_URL}}` |
| `AGENT_RUNNER_HMAC_SECRET` | Same secret as on the runner |
| `AGENT_RUNNER_DEFAULT_TIMEOUT_SECONDS` | `1200` (optional override) |

The migration `a1b2c3d4e5f6_add_agent_runs_tables` runs automatically on the
next API deploy (the existing release process applies pending migrations).

## 5. Frontend (Vercel)

No new env vars on the frontend. The Build modal and the BuildRunDrawer talk to
the same `/api/v1/...` paths as everything else. Deploy `continuum-MVP` once
the backend is updated.

## 6. Smoke test

1. Open a task, link a repo + branch under **Development**.
2. Click **Build**.
3. Pick the branch + mode + (optional) instructions, then **Start build**.
4. The right-hand drawer should open and show, in order:
   - `Started`
   - `Fetching GitHub token`
   - `Cloning repository`
   - `Workspace ready`
   - `Agent loop started`
   - One or more `tool_call` / `tool_result` cards
   - A `commit` card (with PR link if `mode=open_pr`)
   - A `final_message` summary
5. Status pill flips to `Succeeded`. `View PR` / `View commit` links work.

## 7. Operating notes

- **Concurrency**: the API enforces `AGENT_MAX_ACTIVE_PER_PROJECT=1` via a
  partial unique index; the runner can still run multiple unrelated projects
  in parallel up to `RUNNER_CONCURRENCY`.
- **Workspace cleanup**: per-run dirs under `/work/<run_id>` are removed in a
  `finally` block. If the runner OOM-kills mid-run, Railway recreates the
  container; old workspaces don't persist across restarts.
- **Abandoned runs**: if the runner dies after it claimed a job from the
  Redis stream, `xautoclaim` picks it back up after 60 s of idle time so the
  job isn't lost.
- **Cost guardrails**: tweak `MAX_ITERATIONS`, `MAX_WALL_CLOCK_SECONDS`, and
  `MAX_TOKENS_PER_RUN` env vars on the runner.
- **Cancelling**: clicking *Cancel run* in the drawer flips the DB row and
  publishes `{"kind":"cancel"}` on the per-run control channel. The runner
  picks it up at its next checkpoint between LLM turns.
