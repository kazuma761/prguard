"""Build what every command needs: repo config, the PR, its diff, and check results.

All GitHub reads go through the policy gateway under trusted roles
(prguard-config, prguard-orchestrator). Nothing here comes from a model.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend import diffs, repo_config
from backend.agents import prompts
from backend.config import settings
from backend.events import bus
from backend.gateway.base import ToolGateway
from backend.models import Denial, Event, TaskContext, UpstreamError
from backend.repo_config import RepoConfig
from backend.sandbox import runner
from backend.sandbox.checkout import checkout_pr

ORCHESTRATOR = "prguard-orchestrator"
CONFIG_AGENT = "prguard-config"
MAX_HEAD_FILES = 25
MAX_HEAD_FILE_BYTES = 200_000


@dataclass
class Options:
    """How a command runs. Set by the CLI, the server or the tests; never by PR content."""

    publish: bool = True                 # post to GitHub (False = dry run, reports only)
    sandbox: bool = False                # run the PR's checks in the locked container
    checks_file: Path | None = None      # or: use the pipeline's own lint/test output
    ci_log: Path | None = None           # failed CI step log for the CI fixer
    workdir: Path | None = None          # existing checkout of the PR head (CI)
    question: str = ""                   # /ask


def _text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        return str(data.get("content") or data.get("text") or "")
    return ""


async def load_config(ctx: TaskContext, gateway: ToolGateway) -> RepoConfig:
    """`.prguard.toml` from the base branch, or defaults."""
    try:
        res = await gateway.call(CONFIG_AGENT, ctx, "get_file_contents",
                                 {"repo": ctx.repo_full_name, "path": ".prguard.toml", "ref": ctx.base_ref})
    except UpstreamError:
        return repo_config.defaults()
    if isinstance(res, Denial):
        cfg = repo_config.defaults()
        cfg.warnings.append(f"could not read .prguard.toml: {res.reason}")
        return cfg
    return repo_config.parse(_text(res.data), source=f".prguard.toml@{ctx.base_ref}")


def prompt_budget(reserve_output: int | None = None, overhead: int = 1100) -> int:
    """Tokens left for the diff after the system prompt, check output and the answer."""
    reserve = reserve_output if reserve_output is not None else settings.llm_max_output_tokens
    return max(800, settings.llm_context_tokens - reserve - overhead)


async def build(ctx: TaskContext, cfg: RepoConfig, gateway: ToolGateway, options: Options,
                *, want_head_files: bool = True, want_checks: bool = False) -> dict[str, Any]:
    scope = {"repo": ctx.repo_full_name, "pull_number": ctx.pr_number}
    pr_res = await gateway.call(ORCHESTRATOR, ctx, "get_pull_request", scope)
    pr = pr_res.data if not isinstance(pr_res, Denial) and isinstance(pr_res.data, dict) else {}

    files_res = await gateway.call(ORCHESTRATOR, ctx, "get_pull_request_files", scope)
    files = files_res.data if not isinstance(files_res, Denial) and isinstance(files_res.data, list) else []
    if not files:
        try:
            diff_res = await gateway.call(ORCHESTRATOR, ctx, "get_pull_request_diff", scope)
            files = diffs.split_unified_diff(_text(diff_res.data)) if not isinstance(diff_res, Denial) else []
        except UpstreamError:
            files = []
    diff = "\n".join(f"--- a/{f['filename']}\n+++ b/{f['filename']}\n{f.get('patch', '')}" for f in files)

    prompt_diff = diffs.build_prompt_diff(files, cfg.ignore, prompt_budget())
    small = diffs.build_prompt_diff(files, cfg.ignore, prompt_budget(overhead=3200))

    head_files: dict[str, str] = {}
    if want_head_files:
        for item in [f for f in files if f["filename"] in prompt_diff.included][:MAX_HEAD_FILES]:
            try:
                res = await gateway.call(ORCHESTRATOR, ctx, "get_file_contents",
                                         {"repo": ctx.repo_full_name, "path": item["filename"], "ref": ctx.head_sha})
            except UpstreamError:
                continue
            if not isinstance(res, Denial):
                text = _text(res.data)
                if len(text) <= MAX_HEAD_FILE_BYTES:
                    head_files[item["filename"]] = text

    check_results: dict[str, Any] = runner.skipped("checks not requested")
    if want_checks:
        check_results = await _checks(ctx, cfg, options)

    return {
        "config": cfg, "pr": pr, "title": pr.get("title", ""), "body": pr.get("body", ""),
        "files": files, "diff": diff, "prompt_diff": prompt_diff.text, "prompt_diff_small": small.text,
        "diff_note": prompt_diff.note(), "head_files": head_files, "check_results": check_results,
        "ci_log": options.ci_log.read_text(encoding="utf-8", errors="replace") if options.ci_log else "",
    }


async def _checks(ctx: TaskContext, cfg: RepoConfig, options: Options) -> dict[str, Any]:
    if options.checks_file:
        return runner.from_file(options.checks_file)
    if not options.sandbox:
        return runner.skipped("sandbox disabled; no pipeline check output given")
    try:
        src = await asyncio.wait_for(checkout_pr(ctx, options.workdir), timeout=180)
        return await runner.run_checks(src, ctx, cfg.checks)
    except Exception as exc:  # a failed checkout must not sink the review
        bus.emit(Event(type="error", review_id=ctx.review_id, source="sandbox", reason=str(exc)[:300]))
        return runner.skipped(f"checkout failed: {exc}")


def pr_prompt(context: dict[str, Any], *, small: bool = False, with_checks: bool = False, body: bool = False) -> str:
    return prompts.pr_block(context["prompt_diff_small" if small else "prompt_diff"],
                            context["check_results"] if with_checks else None,
                            title=context.get("title", ""), body=context.get("body", "") if body else "")
