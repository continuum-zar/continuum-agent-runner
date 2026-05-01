FROM python:3.12-slim

# System packages: git for clone/push, curl for healthchecks/debug, ca-certificates,
# build-essential for any C extensions, nodejs/npm so the agent can run JS toolchains
# in the cloned repo, jq for shell tool ergonomics.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        ripgrep \
        build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# pnpm + yarn for repos that prefer them
RUN npm install -g pnpm@9 yarn@1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY runner /app/runner
COPY README.md /app/README.md

# Per-run workspaces live here (override with WORKSPACE_ROOT)
RUN mkdir -p /work
ENV WORKSPACE_ROOT=/work
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "runner.main"]
