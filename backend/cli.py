"""PRGuard command line: the entry point for CI/CD pipelines.

  prguard review   --repo owner/name --pr 123        # review + CI gate (exit 1 = gate failed)
  prguard describe --pr-url https://github.com/o/r/pull/123
  prguard improve  --repo owner/name --pr 123
  prguard ask      --repo owner/name --pr 123 "is this backwards compatible?"
  prguard run      --event-path $GITHUB_EVENT_PATH   # GitHub Actions: PR events and /commands
  prguard serve                                       # webhook server + dashboard
  prguard doctor                                      # check model server, GitHub token, sandbox
  prguard eval                                        # score the configured models on labelled PRs

Exit codes: 0 = passed, 1 = the CI gate failed, 2 = usage or setup error.
Reports land in --out (default ./prguard-out): prguard-report.json, prguard.sarif, prguard-summary.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from backend.config import settings
from backend.models import SEVERITY_RANK, CommandResult


class UsageError(Exception):
    """Bad arguments or setup; exit code 2."""


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", type=Path, default=Path("prguard-out"), help="directory for JSON/SARIF/Markdown reports")
    p.add_argument("--no-publish", action="store_true", help="dry run: do not post anything to GitHub")
    p.add_argument("--mock", action="store_true", help="offline demo: mock GitHub fixtures and mock model")
    p.add_argument("--sandbox", action="store_true", help="run the PR's linters/tests in the locked Docker sandbox")
    p.add_argument("--checks-file", type=Path, help="use this lint/test output from your pipeline instead of the sandbox")
    p.add_argument("--ci-log", type=Path, help="log of a failed CI step, for the CI fixer")
    p.add_argument("--workdir", type=Path, help="existing checkout of the PR head (used with --sandbox)")
    p.add_argument("--fail-on", help="override [gate] fail_on: CRITICAL|HIGH|MEDIUM|LOW|INFO|none")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")


def _target(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name (default: $GITHUB_REPOSITORY)")
    p.add_argument("--pr", type=int, help="pull request number")
    p.add_argument("--pr-url", help="https://github.com/owner/name/pull/123")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prguard", description="Local, policy-guarded AI pull-request reviewer.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("review", "review the PR and gate the pipeline"), ("describe", "write the PR description"),
                            ("improve", "post inline code suggestions"), ("ask", "answer a question about the PR")):
        p = sub.add_parser(name, help=help_text)
        _target(p)
        _common(p)
        if name == "ask":
            p.add_argument("question", nargs="+")
    p = sub.add_parser("run", help="handle a GitHub event (pull_request or issue_comment)")
    p.add_argument("--event-path", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    p.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    _common(p)
    p = sub.add_parser("serve", help="webhook server and dashboard")
    p.add_argument("--host", default=os.environ.get("PRGUARD_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PRGUARD_PORT", "8000")))
    sub.add_parser("doctor", help="check the model server, GitHub token and sandbox")
    p = sub.add_parser("eval", help="score the configured models on the labelled demo PRs")
    p.add_argument("--out", type=Path, default=Path("prguard-out"))
    p.add_argument("--runs", type=int, default=1, help="repeat each PR this many times")
    return parser


def _setup(args: argparse.Namespace) -> None:
    if getattr(args, "mock", False):
        settings.upstream = "mock"
        settings.llm_provider = "mock"
    from backend.gateway import reset_gateways

    reset_gateways()


def _options(args: argparse.Namespace, question: str = ""):
    from backend.context import Options

    for path in (args.checks_file, args.ci_log):
        if path and not path.is_file():
            raise UsageError(f"{path} does not exist")
    return Options(publish=not args.no_publish, sandbox=args.sandbox, checks_file=args.checks_file,
                   ci_log=args.ci_log, workdir=args.workdir, question=question)


def _annotate(result: CommandResult, threshold: str) -> None:
    """GitHub Actions annotations on the diff for each finding."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or not result.review:
        return
    rank = SEVERITY_RANK.get(threshold, 99)
    for f in result.review.findings[:30]:
        level = "error" if SEVERITY_RANK[f.severity] >= rank else "warning" if f.severity in ("MEDIUM", "HIGH", "CRITICAL") else "notice"
        where = f"file={f.file_path},line={max(1, f.line_start)}," if f.file_path else ""
        message = f"{f.summary} — {f.suggestion}".replace("%", "%25").replace("\r", "").replace("\n", " ")
        print(f"::{level} {where}title=PRGuard {f.severity} {f.category}::{message}")


def _print(results: list[CommandResult], paths: dict[str, str], as_json: bool) -> None:
    if as_json:
        print(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
        return
    for r in results:
        status = "ok" if r.ok else f"ERROR: {r.error}"
        print(f"prguard {r.command} {r.repo}#{r.pr_number} @{r.head_sha[:7]}  {r.duration_s:.1f}s  {status}")
        if r.review:
            print(f"  verdict {r.review.verdict} · {len(r.review.findings)} finding(s) · confidence {r.review.overall_confidence:.2f}")
            for f in r.review.findings:
                print(f"   {f.severity:8} {f.agent_type:8} {f.file_path}:{f.line_start}  {f.summary[:90]}  [{f.checker_status}]")
            if r.review.checker_notes:
                print(f"  checker: {r.review.checker_notes}")
        if r.description:
            print(f"  {r.description.summary[:300]}")
        if r.suggestions:
            for s in r.suggestions:
                print(f"   {s.importance:>2}/10 {s.label:15} {s.file_path}:{s.line_start}  {s.summary[:90]}")
        if r.answer:
            print("  " + r.answer[:600].replace("\n", "\n  "))
        for url in r.posted_urls:
            print(f"  posted: {url}")
        if r.gate:
            print(f"  gate: {'FAILED' if r.gate.get('failed') else 'passed'} — {r.gate.get('reason', '')}")
    print(f"reports: {paths.get('json')}, {paths.get('sarif')}, {paths.get('markdown')}")


async def _run_commands(ctx, commands: list[str], options, cfg, fail_on: str | None) -> list[CommandResult]:
    from backend import commands as cmds

    if fail_on:
        cfg.gate.fail_on = fail_on.upper()
    results = []
    for command in commands:
        results.append(await cmds.run(ctx, command, options, cfg))
    return results


async def _direct(args: argparse.Namespace) -> list[CommandResult]:
    from backend import triggers
    from backend.context import load_config
    from backend.gateway import gateway

    if args.pr_url:
        parsed = triggers.parse_pr_url(args.pr_url)
        if not parsed:
            raise UsageError("--pr-url must look like https://github.com/owner/name/pull/123")
        repo, number = parsed
    else:
        repo, number = args.repo, args.pr
    if args.mock:
        repo = repo or settings.target_repo
        if repo != settings.target_repo:
            settings.target_repo = repo
    if not repo or not number:
        raise UsageError("give --repo and --pr, or --pr-url")
    if not args.mock and not settings.github_token:
        raise UsageError("GITHUB_TOKEN is not set")
    gw = gateway()
    ctx = await triggers.context_for_pr(gw, repo, number)
    cfg = await load_config(ctx, gw)
    question = " ".join(getattr(args, "question", []) or [])
    return await _run_commands(ctx, [args.cmd], _options(args, question), cfg, args.fail_on)


async def _event(args: argparse.Namespace) -> list[CommandResult]:
    from backend import triggers
    from backend.gateway import gateway

    if not args.event_path or not Path(args.event_path).is_file():
        raise UsageError("--event-path (or $GITHUB_EVENT_PATH) must point to the event JSON")
    payload = json.loads(Path(args.event_path).read_text(encoding="utf-8"))
    event = args.event_name or ("issue_comment" if "comment" in payload else "pull_request")
    job, reason = await triggers.job_from_event(event, payload, gateway())
    if job is None:
        print(f"prguard: nothing to do ({reason})")
        return []
    print(f"prguard: {job.trigger} -> {', '.join(job.commands)}")
    return await _run_commands(job.ctx, job.commands, _options(args, job.question), job.cfg, args.fail_on)


def _finish(results: list[CommandResult], args: argparse.Namespace) -> int:
    from backend import reports

    if not results:
        return 0
    paths = reports.write(results, args.out)
    for r in results:
        threshold = str(r.gate.get("fail_on", "HIGH"))
        _annotate(r, threshold)
    _print(results, paths, args.json)
    return 1 if any(r.gate.get("failed") for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("backend.main:app", host=args.host, port=args.port, log_level="info")
        return 0
    if args.cmd == "doctor":
        from backend.doctor import doctor

        return asyncio.run(doctor())
    if args.cmd == "eval":
        from backend.evaluation import evaluate

        return asyncio.run(evaluate(args.out, args.runs))
    _setup(args)
    try:
        results = asyncio.run(_event(args) if args.cmd == "run" else _direct(args))
    except UsageError as exc:
        print(f"prguard: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # setup problems (bad token, PR not found) are exit 2, not a gate failure
        print(f"prguard: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return _finish(results, args)


if __name__ == "__main__":
    sys.exit(main())
