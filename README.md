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
| `LLM_EXPLORE_MODEL`, `LLM_SYNTHESIZE_MODEL` | Route cheap search/read turns to a high-limit model and write/commit turns to the stronger model |
| `LLM_SYNTHESIZE_TOOLS` | Comma-separated tool names that permanently switch the run from explore to synthesize mode |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` | App credentials so the runner can mint installation tokens locally |
| `RUNNER_CONCURRENCY` | Worker tasks per process (default 2) |
| `JOB_STALE_IDLE_MS` | How long a pending Redis stream job must be idle before another worker may reclaim it (default 1200000) |
| `WORKSPACE_ROOT` | Directory for per-run workspaces (default `/work`) |
| `HISTORY_COMPACT_TOKEN_THRESHOLD` | Estimated-token threshold before old large tool results are compacted (default 60000) |
| `HISTORY_KEEP_RECENT_TOOL_RESULTS` | Recent tool results to keep verbatim during compaction (default 4) |

## Tools the agent has

| tool | purpose |
|------|---------|
| `list_dir` | List files in a directory inside the workspace |
| `glob_files` | Find tracked files by glob pattern |
| `grep_files` | Search file contents with ripgrep |
| `read_file` | Read a file (capped at 200 KB) |
| `read_many_files` | Read several files in one tool call |
| `write_file` | Write or overwrite a file |
| `apply_patch` | Apply a unified diff |
| `run_shell` | Run an allow-listed shell command |
| `git_status` | `git status --porcelain` |
| `git_diff` | `git diff` (working tree or staged) |
| `commit_and_push` | `git add` + `git commit` + `git push` (and create PR if mode=open_pr) |
| `done` | Finish the run with a final summary |

All tools operate inside the per-run workspace; absolute paths or `..` traversal
out of the workspace are rejected.

## Sandbox & security model

The runner executes commands produced by an LLM against a freshly-cloned git
repository. Because that input is **untrusted** (a prompt-injected README, an
adversarial issue body, or a model mistake can all turn into a shell command),
this section spells out exactly what isolation the runner provides today and
what it does not.

### What we rely on

1. **The Docker container itself.** The runner ships as a single image
   (`Dockerfile`) and on Railway runs as a regular Linux container. The
   process is non-root inside the container, and the container has no host
   bind-mounts.
2. **A per-run workspace under `/work/<run_id>`.** Each run gets its own
   directory; `Workspace.resolve()` (in `runner/sandbox/workspace.py`) rejects
   any path that resolves outside that directory. The directory is `rmtree`d
   in a `finally` block when the run ends.
3. **A stripped environment for child processes.** `_strip_env()` in
   `runner/sandbox/shell.py` keeps only the `$PATH`/`$HOME`/locale-style vars
   a normal toolchain needs and drops everything else — in particular it
   strips `LLM_API_KEY`, `AGENT_RUNNER_HMAC_SECRET`, `GITHUB_APP_PRIVATE_KEY`,
   and any one-shot clone tokens so they are not visible to commands the
   agent runs.
4. **A command allow-list + deny regex** in `runner/sandbox/shell.py`
   (`_ALLOWED_CMDS`, `_DENY_RE`, `_ENV_DUMP_RE`, `_RM_RF_ABSOLUTE_RE`,
   `_ABSOLUTE_PATH_GUARD_CMDS`). Every agent-supplied `run_shell` call goes
   through `validate_command()`; `bash -c` / `bash -lc` bodies are
   re-validated with the same rules.
5. **An output cap and a wall-clock timeout** so a runaway command can't
   exhaust memory or hang the worker (`MAX_SHELL_OUTPUT_BYTES`,
   `MAX_SHELL_TIMEOUT_SECONDS`).

### What we do NOT rely on

- **There is no gVisor, no Firecracker, and no per-run seccomp profile.**
  Multiple runs share one container up to `RUNNER_CONCURRENCY` (default 2).
  The kernel boundary between runs is the same kernel boundary the host OS
  provides — nothing finer-grained.
- **There is no network egress filter.** `pip install` / `npm install` /
  `git clone` all need internet access, so the container has it. `curl`,
  `wget`, and similar are denied via regex but a language runtime
  (`python -c "import urllib.request; …"`) can still reach the network.
- **The allow-list does not stop arbitrary code execution.** We deliberately
  allow `python`, `node`, `pip`, `npm`, `pnpm`, `yarn`, `cargo`, `go`,
  `make`, `gradle`, `mvn`, `bash -c` … because the agent's job is to build
  and test arbitrary repos. Any of those can execute downloaded code
  (`pip install`, `npm install`), so a determined adversary with control of
  the agent prompt can run arbitrary native code inside the container.

### Threat model

The allow-list + deny regex are aimed at the **LLM-mistake threat model**, not
the **adversarial-human threat model**:

| Threat | Mitigated by |
|--------|--------------|
| Model hallucinates `rm -rf /` or `rm -rf ~` | `_RM_RF_ABSOLUTE_RE` + `_ABSOLUTE_PATH_GUARD_CMDS` |
| Model tries to `curl`/`wget`/`ssh`/`scp` data out | `_DENY_RE` (network egress block) |
| Prompt-injected README tries to dump the runner's env vars | `_ENV_DUMP_RE` (bare `env` / `printenv`), `_DENY_RE` (`/proc/<pid>/environ`, `~/.ssh`, `.aws/credentials`, `.netrc`, `.git-credentials`) |
| Prompt-injected README tries to open a reverse shell | `_DENY_RE` (`/dev/tcp/`, `/dev/udp/`, `mkfifo`, `bash -i`) |
| Model tries to mutate the host (`sudo`, `mount`, `chown`, `useradd`, `systemctl`, setuid `chmod`) | `_DENY_RE` (privilege/host-mutation set) |
| Model tries to read another run's workspace via absolute path | `_ABSOLUTE_PATH_GUARD_CMDS` rejects abs/`~` args to `rm`/`cp`/`mv`/`ln`/`mkdir`/`touch`/`chmod`; `Workspace.resolve()` blocks traversal in file tools |
| Model tries to `chmod u+s` or numeric `4xxx` to install a setuid binary | `_DENY_RE` (setuid/setgid bit on `chmod`) |
| Model `eval`s a string or `source`s a file it just wrote | `_DENY_RE` (`eval `, `source `, `. /…`) |
| Single tool call exhausts memory or hangs forever | `MAX_SHELL_OUTPUT_BYTES`, `MAX_SHELL_TIMEOUT_SECONDS` |
| Single run consumes unbounded API tokens / wall-clock | `MAX_ITERATIONS`, `MAX_WALL_CLOCK_SECONDS`, `MAX_TOKENS_PER_RUN` |

Threats we accept (i.e. the deploy operator must decide whether they are
acceptable):

- A determined attacker who controls the prompt can still execute arbitrary
  code inside the container via `pip`/`npm`/`python -c`.
- Two concurrent runs on the same container can in principle observe each
  other's processes via `/proc` (file paths are blocked but pid listings are
  not). If this matters for your deployment, set `RUNNER_CONCURRENCY=1`.
- Outbound network traffic to attacker-controlled hosts via a language
  runtime is not blocked. If this matters, run the container behind an
  egress firewall that only allows your package mirrors and `github.com`.

### How to add real isolation

If the threat model above is not strong enough for your deployment, the two
places to add isolation are:

1. **Per-run sandbox.** Wrap each `run_shell_capped` invocation (and the
   workspace clone in `workspace.py`) in `gvisor-runsc`, `firecracker`, a
   nested unprivileged container, or a Kubernetes `Job` with a strict
   `seccomp`/`AppArmor` profile. This is the single biggest hardening
   improvement available; everything else in this section is defense in
   depth around it.
2. **Egress firewall.** Run the container behind an egress proxy / NAT that
   only permits the package indices, git hosts, and LLM endpoints you
   actually use. The runner's own outbound calls are limited to
   `BACKEND_URL`, the LLM provider, GitHub, and the configured package
   mirrors, so a default-deny egress policy is realistic.

### Allow-list rationale (current set)

| Bucket | Commands | Why kept |
|--------|----------|----------|
| Read/inspect | `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg`, `find`, `echo`, `true`, `false`, `pwd`, `which`, `stat`, `file`, `diff`, `sort`, `uniq`, `sed`, `awk`, `tr`, `basename`, `dirname`, `realpath`, `date`, `tree`, `jq`, `xargs` | The agent's read-only exploration toolkit. Side-effect-free, or only mutate stdout. |
| Workspace file mutation | `cp`, `mv`, `mkdir`, `rmdir`, `rm`, `touch`, `ln` | The agent has dedicated `write_file` / `apply_patch` tools for content edits, but still legitimately needs to move/delete files (deleting `node_modules`, renaming a directory). Restricted to workspace-relative paths by `_ABSOLUTE_PATH_GUARD_CMDS`. |
| Env-as-prefix | `env`, `printenv` | `env FOO=bar cmd` is a common build idiom and `printenv FOO` is sometimes useful for debugging. Bare `env` / `printenv` (which would dump every secret in the runner's process env) is blocked by `_ENV_DUMP_RE`. |
| Archives | `tar`, `zip`, `unzip`, `gzip`, `gunzip` | Required by some build pipelines. Can only repack workspace contents because the network-egress commands they would pipe to (`curl`, `scp`, …) are denied. |
| Git | `git` | The agent's primary tool for status/diff/commit/push/clone. Runs against the workspace; remote operations use the short-lived installation token injected at clone time. |
| Language toolchains | `python`, `python3`, `pip`, `pip3`, `uv`, `poetry`, `pytest`, `ruff`, `mypy`, `black`, `flake8`, `node`, `npm`, `npx`, `pnpm`, `yarn`, `tsc`, `eslint`, `prettier`, `vitest`, `jest`, `go`, `cargo`, `rustc`, `make`, `cmake`, `java`, `javac`, `mvn`, `gradle` | The reason the runner exists — it has to install deps and run tests. These are unavoidably arbitrary-code-execution; see "What we do NOT rely on" above. |
| Shell wrappers | `bash`, `sh` | For piped/compound commands. `-c` / `-lc` bodies are re-validated through `validate_command` with the same allow-list and deny regex. |

Commands considered but **rejected** (see commit history): `yes` (output
flooder, no agent use-case), `tee` (`write_file` covers writes and
`tee /etc/...` is the classic privilege-escalation vector), `less`/`more`
(interactive pagers that hang on non-TTY pipes), `curl`/`wget`/`ssh`/`scp`/
`sftp`/`rsync`/`telnet`/`ftp`/`socat`/`netcat`/`nc`/`ncat` (network egress;
git push and pip install have their own configured endpoints).
