"""Run one PRGuard command (review, describe, improve, ask) on one PR.

Shared by the CLI (CI pipelines), the webhook server and the tests.
"""

from __future__ import annotations

import time
from typing import Any

from backend import context as ctxmod
from backend import reports
from backend.agents import asker, describer, improver, llm
from backend.events import bus
from backend.gateway import gateway
from backend.models import CommandResult, Event, TaskContext
from backend.orchestrator import graph
from backend.repo_config import COMMANDS, RepoConfig

SOURCE = "pr"


def _span(ctx: TaskContext, kind: str, **payload: Any) -> None:
    bus.emit(Event(type=kind, review_id=ctx.review_id, source=SOURCE, payload=payload))


async def run(ctx: TaskContext, command: str, options: ctxmod.Options, cfg: RepoConfig | None = None) -> CommandResult:
    command = command.lower().lstrip("/")
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command!r}; expected one of {', '.join(COMMANDS)}")
    ctx = ctx.model_copy(update={"command": command})
    gw = gateway()
    started = time.monotonic()
    _span(ctx, "span.start", command=command, repo=ctx.repo_full_name, pr=ctx.pr_number, head_sha=ctx.head_sha,
          enforcer="POLICY")
    cfg = cfg or await ctxmod.load_config(ctx, gw)
    result = CommandResult(command=command, review_id=ctx.review_id, repo=ctx.repo_full_name,
                           pr_number=ctx.pr_number, head_sha=ctx.head_sha)
    try:
        if command == "review":
            review, errors = await graph.run_review(ctx, {"config": cfg}, options)
            result.review = review
            result.posted_urls = [review.posted_url] if review.posted_url else []
            result.gate = reports.gate(review, cfg.gate)
            ran = len(cfg.review.specialists) + (1 if cfg.review.ci_fixer else 0)
            reviewers_failed = [a for a in errors if a != "ci"]
            if cfg.review.specialists and len(reviewers_failed) == len(cfg.review.specialists):
                # Every reviewer failed (model down?): no findings must not read as "clean".
                result.ok = False
                result.error = "all reviewers failed: " + "; ".join(f"{a}: {e}" for a, e in sorted(errors.items()))
            elif errors and ran:
                result.usage["partial_failures"] = errors
        else:
            context = await ctxmod.build(ctx, cfg, gw, options, want_head_files=(command == "improve"))
            if command == "describe":
                result.description, result.posted_urls, result.usage = await describer.run(ctx, context, options, gw)
            elif command == "improve":
                result.suggestions, result.posted_urls, result.usage = await improver.run(ctx, context, options, gw)
            else:
                result.answer, result.posted_urls, result.usage = await asker.run(ctx, context, options, gw)
    except (llm.LLMError, ValueError) as exc:
        result.ok, result.error = False, str(exc)[:500]
    except Exception as exc:  # never crash a pipeline without saying why
        result.ok, result.error = False, f"{type(exc).__name__}: {str(exc)[:400]}"

    if not result.ok:
        result.gate = {"failed": cfg.gate.fail_on_error, "error": True,
                       "reason": f"PRGuard could not finish {command}: {result.error}"
                                 + ("" if cfg.gate.fail_on_error else " (passing because fail_on_error = false)")}
        bus.emit(Event(type="error", review_id=ctx.review_id, source=SOURCE, reason=result.error))
    elif not result.gate:
        result.gate = {"failed": False, "reason": f"{command} does not gate the pipeline"}
    result.duration_s = round(time.monotonic() - started, 2)
    bus.emit(Event(type="command.done", review_id=ctx.review_id, source=SOURCE,
                   payload=result.model_dump(mode="json", exclude={"review"})))
    _span(ctx, "span.end", command=command, ok=result.ok)
    return result
