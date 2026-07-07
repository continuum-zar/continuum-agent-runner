"""Orchestrator → worker delegation around the Codex coding run.

A large task is not handed to the coding agent in one blind shot. Instead:

1. The ORCHESTRATOR (large model) reads the task and the repo file tree and
   produces an execution plan plus a set of scout assignments.
2. SCOUTS (small model workers) run IN PARALLEL, each exploring one aspect of
   the repo (read-only: file tree, ripgrep, capped file reads) and reporting
   focused findings.
3. The plan + findings are injected into the Codex prompt so the coding agent
   starts with a map instead of exploring from scratch.
4. After Codex finishes, a VERIFIER (small model worker) checks the produced
   diff against the task's checklist and tightens the final summary.

Every delegated step is streamed to the UI as subagent_* events so the user
can watch workers being spawned and working simultaneously. All of this is
best-effort: any failure degrades to the plain single-Codex run.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

from runner.agent.context import RunContext
from runner.config import settings
from runner.llm import chat, chat_json
from runner.logger import get_logger
from runner.sandbox.shell import run_shell_capped

logger = get_logger(__name__)

_MAX_TREE_LINES = 400
_MAX_SCOUT_FILES = 8
_MAX_SCOUT_SEARCHES = 4
_MAX_FILE_BYTES = 6_000
_MAX_RG_OUTPUT = 3_000


# ---------------------------------------------------------------------------
# Worker prompts
# ---------------------------------------------------------------------------

_ORCHESTRATOR_SYSTEM = """\
You are the orchestrator for Continuum's autonomous coding agent. A coding
agent (with full shell access to a cloned repository) will execute a task; you
break the task down first so it starts with a plan and a map instead of
exploring blind.

You receive the task (title, description, checklist, extra instructions) and
the repository file tree. Return STRICT JSON with exactly these keys:

- "execution_plan": array of 3-8 strings. Concrete, ordered implementation
  steps. Each step names the specific files, directories, or symbols involved
  when they can be inferred from the tree — never write vague steps like
  "update the code" or "test everything".
- "scouts": array of 0-{max_scouts} objects, each {{"title": str, "goal": str}}.
  Scouts are small read-only research workers that run in parallel BEFORE the
  coding agent starts. Give each scout ONE narrow, non-overlapping question
  whose answer the coding agent will need, e.g. "Locate the task checklist
  rendering component and list its props" or "Find how SSE events are parsed
  and which event kinds exist". "title" is 2-5 words; "goal" is 1-2 precise
  sentences. Do NOT create scouts for things the task context already answers,
  and do not exceed {max_scouts}.
- "risk_notes": array of 0-4 strings. Only real, repo-specific risks (e.g. a
  shared type that several modules import); omit generic advice.

Base everything on the provided task and tree. Do not invent files that are
not in the tree.
"""

_SCOUT_SELECT_SYSTEM = """\
You are a repo scout worker. You have ONE research goal and the repository
file tree. Choose what to inspect. Return STRICT JSON:

- "files": array of up to {max_files} paths copied EXACTLY from the tree that
  most likely answer your goal.
- "searches": array of up to {max_searches} ripgrep regex patterns (plain
  strings, no flags) to locate symbols or usages relevant to your goal.

Pick the smallest set that answers the goal; prefer source files over docs,
lockfiles, or generated output.
"""

_SCOUT_REPORT_SYSTEM = """\
You are a repo scout worker reporting back to the orchestrator. You receive
your research goal, ripgrep matches, and file excerpts. Write a compact
markdown report, maximum 300 words, with exactly these sections:

## Findings
Bullet list. Every bullet must cite `path:line` or a concrete path from the
provided material. State what IS there, not what might be.

## Relevant files
Bullet list of the files a coding agent should edit or read for this goal,
one per line, with a 5-10 word reason each.

## Gotchas
Bullet list of concrete pitfalls visible in the excerpts (shared types,
validation that must be kept in sync, naming conventions). Write "None found."
if there are none.

Never speculate beyond the provided excerpts; if the material does not answer
the goal, say exactly what is missing.
"""

_VERIFIER_SYSTEM = """\
You are a verification worker. A coding agent just finished a task; you check
its work against the task requirements. You receive the task (title,
description, checklist), the git diff stat of what changed, and the agent's
own summary. Return STRICT JSON:

- "refined_summary": string. The summary rewritten as clean markdown with the
  sections "## What changed" (bullet per meaningful change, naming files),
  "## How it was verified" (exact commands/outcomes taken from the agent's
  summary; if the agent did not verify, state that plainly), and
  "## Follow-ups" (only if there are real ones). Keep every factual claim
  from the agent's summary; NEVER invent changes, test results, or files that
  are not supported by the diff stat or the summary.
- "unmet_checklist_items": array of strings. Checklist items with NO clear
  evidence of completion in the diff stat or summary. When in doubt, include
  the item.
- "confidence": number 0.0-1.0 that the task requirements are fully met.
"""


# ---------------------------------------------------------------------------
# Read-only gathering helpers (runner-executed; workers never touch the shell)
# ---------------------------------------------------------------------------


async def _file_tree(ctx: RunContext) -> str:
    rc, out, _ = await run_shell_capped(
        ["git", "ls-files"],
        cwd=ctx.workspace.root,
        timeout_seconds=30,
    )
    if rc != 0 or not out.strip():
        return ""
    lines = out.strip().splitlines()
    if len(lines) > _MAX_TREE_LINES:
        head = lines[:_MAX_TREE_LINES]
        head.append(f"... ({len(lines) - _MAX_TREE_LINES} more files)")
        return "\n".join(head)
    return "\n".join(lines)


async def _rg(ctx: RunContext, pattern: str) -> str:
    # Model-generated patterns go through the shell allow-list/deny-regex; a
    # pattern that trips policy (raises) just yields no matches for this scout
    # rather than failing the whole worker.
    try:
        rc, out, _ = await run_shell_capped(
            ["rg", "-n", "--max-count", "6", "--max-columns", "200", "-S", pattern, "."],
            cwd=ctx.workspace.root,
            timeout_seconds=30,
        )
    except Exception:  # noqa: BLE001
        return ""
    if rc != 0:
        return ""
    return out[:_MAX_RG_OUTPUT]


def _read_file_capped(root: Path, rel: str) -> str:
    try:
        target = (root / rel).resolve()
        if not str(target).startswith(str(Path(root).resolve())):
            return ""
        if not target.is_file():
            return ""
        return target.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_BYTES]
    except OSError:
        return ""


def _task_brief(ctx: RunContext) -> str:
    job_ctx = ctx.job.context if isinstance(ctx.job.context, dict) else {}
    parts: list[str] = [f"Task #{ctx.job.task_id}"]
    title = job_ctx.get("task_title")
    if isinstance(title, str) and title:
        parts.append(f"Title: {title}")
    desc = job_ctx.get("task_description")
    if isinstance(desc, str) and desc:
        parts.append(f"Description:\n{desc[:3000]}")
    checklists = job_ctx.get("checklists")
    if isinstance(checklists, list) and checklists:
        items = []
        for item in checklists[:50]:
            if isinstance(item, dict) and item.get("text"):
                done = "x" if item.get("done") else " "
                items.append(f"- [{done}] {item['text']}")
        if items:
            parts.append("Checklist:\n" + "\n".join(items))
    if ctx.job.instructions and ctx.job.instructions.strip():
        parts.append(f"Extra instructions:\n{ctx.job.instructions.strip()[:2000]}")
    return "\n\n".join(parts)


def _checklist_lines(ctx: RunContext) -> list[str]:
    job_ctx = ctx.job.context if isinstance(ctx.job.context, dict) else {}
    checklists = job_ctx.get("checklists")
    out: list[str] = []
    if isinstance(checklists, list):
        for item in checklists[:50]:
            if isinstance(item, dict) and item.get("text"):
                out.append(str(item["text"]))
    return out


# ---------------------------------------------------------------------------
# Scouts
# ---------------------------------------------------------------------------


async def _run_scout(
    ctx: RunContext,
    scout_id: str,
    title: str,
    goal: str,
    tree: str,
) -> Optional[str]:
    """One scout worker: select → gather → report. Returns a markdown report."""
    await ctx.events.emit(
        "subagent_started",
        {
            "subagent_id": scout_id,
            "role": "scout",
            "title": title,
            "goal": goal[:500],
            "model": settings.LLM_WORKER_MODEL,
            "parallel": True,
        },
    )
    try:
        selection, tokens = await chat_json(
            model=settings.LLM_WORKER_MODEL,
            system=_SCOUT_SELECT_SYSTEM.format(
                max_files=_MAX_SCOUT_FILES, max_searches=_MAX_SCOUT_SEARCHES
            ),
            user=f"Goal: {goal}\n\nRepository file tree:\n{tree}",
            max_tokens=500,
        )
        ctx.tokens_used += tokens

        files = [
            f for f in (selection.get("files") or []) if isinstance(f, str)
        ][:_MAX_SCOUT_FILES]
        searches = [
            s for s in (selection.get("searches") or []) if isinstance(s, str) and s.strip()
        ][:_MAX_SCOUT_SEARCHES]

        await ctx.events.emit(
            "subagent_update",
            {
                "subagent_id": scout_id,
                "role": "scout",
                "title": title,
                "detail": f"Reading {len(files)} files, running {len(searches)} searches",
            },
        )

        material: list[str] = []
        for pattern in searches:
            matches = await _rg(ctx, pattern)
            if matches:
                material.append(f"### rg matches for `{pattern}`\n```\n{matches}\n```")
        for rel in files:
            content = _read_file_capped(ctx.workspace.root, rel)
            if content:
                material.append(f"### {rel}\n```\n{content}\n```")

        if not material:
            await ctx.events.emit(
                "subagent_completed",
                {
                    "subagent_id": scout_id,
                    "role": "scout",
                    "title": title,
                    "ok": False,
                    "error": "no matching files or search results",
                },
            )
            return None

        report, tokens = await chat(
            model=settings.LLM_WORKER_MODEL,
            system=_SCOUT_REPORT_SYSTEM,
            user=f"Goal: {goal}\n\n" + "\n\n".join(material),
            max_tokens=700,
        )
        ctx.tokens_used += tokens

        await ctx.events.emit(
            "subagent_completed",
            {
                "subagent_id": scout_id,
                "role": "scout",
                "title": title,
                "ok": True,
                "result_preview": report[:400],
            },
        )
        return f"## Scout report: {title}\nGoal: {goal}\n\n{report.strip()}"
    except Exception as exc:  # noqa: BLE001 - scouts are best-effort
        logger.warning(
            "scout.failed", run_id=ctx.job.run_id, scout=title, error=str(exc)
        )
        await ctx.events.emit(
            "subagent_completed",
            {
                "subagent_id": scout_id,
                "role": "scout",
                "title": title,
                "ok": False,
                "error": str(exc)[:300],
            },
        )
        return None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def run_preflight(ctx: RunContext) -> Optional[str]:
    """Orchestrator plan + parallel scouts.

    Returns a markdown block to append to the Codex prompt, or None when
    orchestration is disabled or fails (the run then proceeds unchanged).
    """
    if not settings.ORCHESTRATION_ENABLED or not settings.LLM_API_KEY:
        return None
    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            _preflight_inner(ctx), timeout=settings.SCOUT_PHASE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.warning(
            "orchestration.preflight_timeout",
            run_id=ctx.job.run_id,
            elapsed=round(time.monotonic() - started, 1),
        )
    except Exception as exc:  # noqa: BLE001 - never block the run on orchestration
        logger.warning(
            "orchestration.preflight_failed", run_id=ctx.job.run_id, error=str(exc)
        )
    await ctx.events.emit(
        "status", {"phase": "orchestration_skipped", "reason": "preflight failed"}
    )
    return None


async def _preflight_inner(ctx: RunContext) -> Optional[str]:
    tree = await _file_tree(ctx)
    if not tree:
        return None

    await ctx.events.emit(
        "status",
        {"phase": "orchestrator_planning", "model": settings.LLM_ORCHESTRATOR_MODEL},
    )

    plan_obj, tokens = await chat_json(
        model=settings.LLM_ORCHESTRATOR_MODEL,
        system=_ORCHESTRATOR_SYSTEM.format(max_scouts=settings.MAX_SCOUT_WORKERS),
        user=f"{_task_brief(ctx)}\n\nRepository file tree:\n{tree}",
        max_tokens=1200,
    )
    ctx.tokens_used += tokens

    plan_steps = [
        s for s in (plan_obj.get("execution_plan") or []) if isinstance(s, str) and s.strip()
    ][:8]
    scouts_raw = [
        s
        for s in (plan_obj.get("scouts") or [])
        if isinstance(s, dict) and s.get("goal")
    ][: settings.MAX_SCOUT_WORKERS]
    risk_notes = [
        s for s in (plan_obj.get("risk_notes") or []) if isinstance(s, str) and s.strip()
    ][:4]

    await ctx.events.emit(
        "status",
        {
            "phase": "orchestrator_plan",
            "items": plan_steps,
            "scout_count": len(scouts_raw),
        },
    )

    if ctx.run_state.cancel_requested:
        return None

    reports: list[str] = []
    if scouts_raw:
        results = await asyncio.gather(
            *(
                _run_scout(
                    ctx,
                    scout_id=f"scout-{i + 1}",
                    title=str(s.get("title") or f"Scout {i + 1}")[:80],
                    goal=str(s.get("goal"))[:600],
                    tree=tree,
                )
                for i, s in enumerate(scouts_raw)
            ),
            return_exceptions=False,
        )
        reports = [r for r in results if r]

    blocks: list[str] = ["# Execution plan (from orchestrator)"]
    blocks.append(
        "The orchestrator decomposed this task and delegated repo research to "
        "parallel scout workers before your run. Follow the plan unless the "
        "code contradicts it; trust the code over the reports."
    )
    if plan_steps:
        blocks.append("\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan_steps)))
    if risk_notes:
        blocks.append("**Risks:**\n" + "\n".join(f"- {r}" for r in risk_notes))
    if reports:
        blocks.append("# Scout findings (parallel workers)")
        blocks.extend(reports)

    if not plan_steps and not reports:
        return None
    return "\n\n".join(blocks)


async def run_verifier(ctx: RunContext, summary: str) -> Optional[str]:
    """Post-Codex verifier worker: refines the summary against the diff.

    Returns a refined summary (possibly with a follow-ups section for unmet
    checklist items), or None to keep the original summary.
    """
    if (
        not settings.ORCHESTRATION_ENABLED
        or not settings.VERIFIER_ENABLED
        or not settings.LLM_API_KEY
        or not summary.strip()
    ):
        return None

    # The verifier runs before the runner commits, so stage everything first
    # (the commit step re-runs `git add -A` anyway) and diff the index — this
    # is the only view that includes new untracked files.
    await run_shell_capped(
        ["git", "add", "-A"], cwd=ctx.workspace.root, timeout_seconds=60
    )
    rc, diff_stat, _ = await run_shell_capped(
        ["git", "diff", "--cached", "--stat"],
        cwd=ctx.workspace.root,
        timeout_seconds=30,
    )
    if rc != 0:
        diff_stat = ""

    subagent_id = "verifier-1"
    await ctx.events.emit(
        "subagent_started",
        {
            "subagent_id": subagent_id,
            "role": "verifier",
            "title": "Verifying against requirements",
            "model": settings.LLM_WORKER_MODEL,
            "parallel": False,
        },
    )
    try:
        checklist = _checklist_lines(ctx)
        user = (
            f"{_task_brief(ctx)}\n\n"
            f"Git diff stat:\n{diff_stat[:3000] or '(no diff available)'}\n\n"
            f"Agent summary:\n{summary[:6000]}"
        )
        result, tokens = await chat_json(
            model=settings.LLM_WORKER_MODEL,
            system=_VERIFIER_SYSTEM,
            user=user,
            max_tokens=1200,
        )
        ctx.tokens_used += tokens

        refined = result.get("refined_summary")
        unmet = [
            u for u in (result.get("unmet_checklist_items") or []) if isinstance(u, str)
        ]
        # Only trust unmet items that actually exist on the checklist.
        unmet = [u for u in unmet if any(u.strip() in c or c in u for c in checklist)]

        await ctx.events.emit(
            "subagent_completed",
            {
                "subagent_id": subagent_id,
                "role": "verifier",
                "title": "Verifying against requirements",
                "ok": True,
                "unmet_count": len(unmet),
                "confidence": result.get("confidence"),
            },
        )

        if not isinstance(refined, str) or not refined.strip():
            return None
        if unmet:
            refined = (
                refined.rstrip()
                + "\n\n## Unverified checklist items\n"
                + "\n".join(f"- {u}" for u in unmet)
            )
        return refined
    except Exception as exc:  # noqa: BLE001 - verifier is best-effort
        logger.warning("verifier.failed", run_id=ctx.job.run_id, error=str(exc))
        await ctx.events.emit(
            "subagent_completed",
            {
                "subagent_id": subagent_id,
                "role": "verifier",
                "title": "Verifying against requirements",
                "ok": False,
                "error": str(exc)[:300],
            },
        )
        return None
