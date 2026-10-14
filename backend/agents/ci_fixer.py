"""CI fixer: explains why CI failed on this commit and proposes a fix.

It can read this repository's workflow runs and job logs and re-run failed
jobs. It has no tool that writes a file, so it cannot touch the pipeline; the
access-control layer denies that for every role anyway.
"""

from __future__ import annotations

import re
from typing import Any

from backend.agents import llm, prompts, specialists
from backend.config import settings
from backend.gateway.base import ToolGateway
from backend.models import Denial, Finding, TaskContext, UpstreamError

AGENT_ID = "prguard-ci-fixer"
# Signs that a failure is infrastructure noise rather than this change. Decided
# by this code, not by a model, because it triggers a real action (a re-run).
_FLAKY = re.compile(
    r"connection reset|etimedout|timed out waiting|temporary failure in name resolution|"
    r"429 too many requests|runner has received a shutdown signal|no space left on device",
    re.I,
)


def _runs(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = data.get("workflow_runs") or data.get("runs") or []
    return [run for run in data if isinstance(run, dict)] if isinstance(data, list) else []


def _log_text(data: Any) -> str:
    """Flatten whatever shape the tool server returns into plain log text."""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("jobs"), list):
            return "\n".join(_log_text(job) for job in data["jobs"])
        return "\n".join(_log_text(data[key]) for key in ("logs", "logs_content", "content", "message") if key in data)
    if isinstance(data, list):
        return "\n".join(_log_text(item) for item in data)
    return ""


async def run_ci_fixer(ctx: TaskContext, context: dict[str, Any], gateway: ToolGateway) -> tuple[list[Finding], dict[str, Any]]:
    """Return (findings, info). Empty when CI is green or not readable.

    In a pipeline, the failed step's log can be handed in directly
    (`prguard review --ci-log FILE`, any CI system); otherwise GitHub Actions
    runs for this exact commit are read through the gateway.
    """
    if context.get("ci_log"):
        return await _explain(ctx, context, context["ci_log"][-12000:], {"source": "ci-log file"})
    scope = {"repo": ctx.repo_full_name}
    try:
        listed = await gateway.call(AGENT_ID, ctx, "list_workflow_runs", {**scope, "head_sha": ctx.head_sha})
    except UpstreamError as exc:
        return [], {"note": f"could not list workflow runs: {str(exc)[:120]}"}
    if isinstance(listed, Denial):
        return [], {"note": f"not allowed to list workflow runs: {listed.reason}"}

    # Trusted code keeps to runs for this exact commit.
    mine = [run for run in _runs(listed.data) if run.get("head_sha") == ctx.head_sha]
    failed = [run for run in mine if run.get("conclusion") == "failure"]
    if not failed:
        return [], {"note": "CI is green for this commit" if mine else "no CI runs for this commit"}

    run = failed[0]
    try:
        logs_res = await gateway.call(AGENT_ID, ctx, "get_job_logs", {**scope, "run_id": run["id"], "head_sha": ctx.head_sha})
    except UpstreamError as exc:
        return [], {"note": f"could not read job logs: {str(exc)[:120]}"}
    if isinstance(logs_res, Denial):
        return [], {"note": f"not allowed to read job logs: {logs_res.reason}"}
    logs = _log_text(logs_res.data)[-12000:]

    info: dict[str, Any] = {"run_id": run["id"], "run_url": run.get("html_url", "")}
    if _FLAKY.search(logs):
        try:
            rerun = await gateway.call(AGENT_ID, ctx, "rerun_failed_jobs", {**scope, "run_id": run["id"]})
            info["rerun"] = "requested" if not isinstance(rerun, Denial) else f"denied: {rerun.reason}"
        except UpstreamError as exc:
            info["rerun"] = f"failed: {str(exc)[:100]}"
        return [Finding(
            agent_type="ci", severity="INFO", category="ci-failure",
            summary="CI failed for a reason that looks transient; failed jobs were re-run",
            rationale="The job log shows an infrastructure error rather than a test or build failure.",
            suggestion="No code change needed if the re-run passes.", confidence=0.6,
        )], info

    return await _explain(ctx, context, logs, info)


async def _explain(ctx: TaskContext, context: dict[str, Any], logs: str, info: dict[str, Any]) -> tuple[list[Finding], dict[str, Any]]:
    if settings.llm_is_mock:
        return mock_findings(logs, context), {**info, "model": "MOCK LLM"}
    cfg = context["config"]
    # Leave room for the log: the diff gets what the log does not use.
    user = (prompts.pr_block(context["prompt_diff_small"], None, title=context.get("title", ""))
            + f"\n\n<ci_logs>\n{prompts.defang(logs[-6000:])}\n</ci_logs>")
    result = await llm.chat(
        prompts.specialist_system("ci", ctx.repo_full_name, ctx.pr_number, ctx.head_sha, max_findings=3,
                                  extra_instructions=cfg.review.extra_instructions), user,
    )
    return specialists.parse_findings(result.text, "ci")[:3], {**info, **result.usage()}


def mock_findings(logs: str, context: dict[str, Any]) -> list[Finding]:
    """Deterministic stand-in: one finding per failing test named in the log."""
    out: list[Finding] = []
    for match in re.finditer(r"^(\S+\.py):(\d+): (\w+)", logs, re.M):
        path, line, error = match.group(1), int(match.group(2)), match.group(3)
        test = re.search(rf"^FAILED {re.escape(path)}::(\w+)", logs, re.M)
        detail = re.search(r"^E\s+(.*)$", logs, re.M)
        out.append(Finding(
            agent_type="ci", severity="HIGH", category="ci-failure",
            summary=f"[MOCK LLM] CI fails: {test.group(1) if test else path} raises {error}",
            file_path=path, line_start=line, line_end=line,
            rationale=f"The failed job log reports: {detail.group(1).strip() if detail else error}.",
            suggestion="Fix the changed code so this test passes again, or update the test if the new behaviour is intended.",
            confidence=0.9,
        ))
    return out[:3]
