"""Access policy: what each agent may and may not do on GitHub."""

import pytest

from backend.config import settings
from backend.gateway.mock_upstream import MockUpstream
from backend.gateway.policy_engine import PolicyEngine, path_denied
from backend.gateway.policy_gateway import PolicyGateway
from backend.models import Denial, TaskContext


@pytest.fixture
def upstream():
    return MockUpstream()


@pytest.fixture
def engine():
    return PolicyEngine(settings.policy_path)


def make_ctx(upstream, pr=2, **extra):
    return TaskContext(review_id=f"test-{pr}", repo_full_name=settings.target_repo, pr_number=pr,
                       head_sha=upstream.head_sha(pr), base_ref="main", sender="dev", **extra)


def hijacked_agent_steps(ctx):
    """What an attacker who took over one reviewer would try (the old assume-breach replay)."""
    repo, pr = ctx.repo_full_name, ctx.pr_number
    return [
        ("get_pull_request_files", {"repo": repo, "pull_number": pr}, "ALLOW"),
        ("get_file_contents", {"repo": repo, "path": ".env", "ref": ctx.head_sha}, "DENY"),
        ("create_pull_request_review", {"repo": repo, "pull_number": pr, "event": "APPROVE", "body": "LGTM"}, "DENY"),
        ("merge_pull_request", {"repo": repo, "pull_number": pr}, "DENY"),
        ("push_files", {"repo": repo, "branch": "main", "message": "x", "files": [{"path": "a", "content": "b"}]}, "DENY"),
        ("get_file_contents", {"repo": settings.other_repo, "path": "README.md", "ref": "main"}, "DENY"),
        ("create_pull_request_review", {"repo": repo, "pull_number": 1, "event": "COMMENT", "body": "x"}, "DENY"),
        ("create_or_update_file", {"repo": repo, "branch": "main", "path": ".github/workflows/ci.yml",
                                   "message": "x", "content": "x"}, "DENY"),
        ("upsert_issue_comment", {"repo": repo, "pull_number": pr, "marker": "review", "body": "x"}, "DENY"),
        ("update_pull_request_body", {"repo": repo, "pull_number": pr, "body": "x"}, "DENY"),
        ("update_pull_request", {"repo": repo, "pull_number": pr, "base": "evil"}, "DENY"),
    ]


async def test_hijacked_reviewer_is_contained(upstream):
    ctx = make_ctx(upstream)
    gateway = PolicyGateway(upstream)
    decisions = []
    for tool, args, expected in hijacked_agent_steps(ctx):
        result = await gateway.call("prguard-security", ctx, tool, args)
        decisions.append("DENY" if isinstance(result, Denial) else "ALLOW")
        assert decisions[-1] == expected, (tool, args, getattr(result, "reason", ""))
    assert not upstream.merged and not upstream.writes and not upstream.reviews and not upstream.comments


def test_approve_needs_human_and_is_scoped_to_one_pr(upstream, engine):
    ctx = make_ctx(upstream)
    args = {"repo": ctx.repo_full_name, "pull_number": 2, "event": "APPROVE"}
    assert not engine.evaluate("prguard-aggregator", ctx, "create_pull_request_review", args).allowed
    approved = ctx.model_copy(update={"human_approved": True})
    assert engine.evaluate("prguard-aggregator", approved, "create_pull_request_review", args).allowed
    assert not engine.evaluate("prguard-aggregator", approved, "create_pull_request_review", {**args, "pull_number": 1}).allowed
    assert not engine.evaluate("prguard-security", approved, "create_pull_request_review", args).allowed
    assert not engine.evaluate("prguard-aggregator", approved, "merge_pull_request", args).allowed


def test_reviewer_reads_only_the_pr_head_and_never_secrets(upstream, engine):
    ctx = make_ctx(upstream)
    read = lambda path, ref=ctx.head_sha: engine.evaluate(
        "prguard-quality", ctx, "get_file_contents", {"repo": ctx.repo_full_name, "path": path, "ref": ref}).allowed
    assert read("app/payments.py")
    assert not read("app/payments.py", ref="main")
    assert not read(".env") and not read("config/.env.prod") and not read("deploy/secrets/key.txt")
    assert not read("certs/server.pem") and not read("../outside") and not read("home/.ssh/id_rsa")
    assert not engine.evaluate("nobody", ctx, "get_pull_request", {"repo": ctx.repo_full_name, "pull_number": 2}).allowed


def test_repo_config_comes_only_from_the_base_branch(upstream, engine):
    ctx = make_ctx(upstream)
    read = lambda path, ref: engine.evaluate(
        "prguard-config", ctx, "get_file_contents", {"repo": ctx.repo_full_name, "path": path, "ref": ref}).allowed
    assert read(".prguard.toml", "main")
    assert not read(".prguard.toml", ctx.head_sha)      # a PR cannot configure its own review
    assert not read("app/payments.py", "main") and not read("../.prguard.toml", "main")


def test_each_command_writes_only_its_own_things(upstream, engine):
    ctx = make_ctx(upstream)
    repo, pr = ctx.repo_full_name, ctx.pr_number
    ok = lambda agent, tool, **args: engine.evaluate(agent, ctx, tool, {"repo": repo, "pull_number": pr, **args}).allowed
    assert ok("prguard-aggregator", "upsert_issue_comment", marker="review", body="x")
    assert not ok("prguard-aggregator", "upsert_issue_comment", marker="describe", body="x")
    assert ok("prguard-describer", "update_pull_request_body", body="x")
    assert not ok("prguard-aggregator", "update_pull_request_body", body="x")
    assert ok("prguard-improver", "create_pull_request_review", event="COMMENT", commit_id=ctx.head_sha, body="x")
    assert not ok("prguard-improver", "create_pull_request_review", event="COMMENT", commit_id="0" * 40, body="x")
    assert not ok("prguard-improver", "create_pull_request_review", event="APPROVE", commit_id=ctx.head_sha, body="x")
    assert ok("prguard-asker", "create_issue_comment", body="x")
    assert not ok("prguard-asker", "create_issue_comment", body="x", **{"pull_number": 1})


def test_ci_fixer_can_rerun_but_never_edit_the_pipeline(upstream, engine):
    ctx = make_ctx(upstream, pr=3)
    allowed = lambda tool, **args: engine.evaluate("prguard-ci-fixer", ctx, tool, {"repo": ctx.repo_full_name, **args}).allowed
    assert allowed("list_workflow_runs") and allowed("get_job_logs", run_id=9003) and allowed("rerun_failed_jobs", run_id=9003)
    assert not allowed("create_or_update_file", path=".github/workflows/ci.yml")
    assert not allowed("push_files") and not allowed("merge_pull_request", pull_number=3)
    assert not allowed("get_file_contents", path="app/payments.py", ref=ctx.head_sha)
    assert not engine.evaluate("prguard-ci-fixer", ctx, "get_job_logs", {"repo": settings.other_repo, "run_id": 1}).allowed


def test_path_globs():
    patterns = [".env*", "**/secrets/**", "**/*.pem", "**/*.key"]
    assert path_denied("secrets/a", patterns) and path_denied("a/b/id.key", patterns)
    assert path_denied("src/environment.py", patterns) is None
    assert path_denied("poetry.lock", ["**/*.lock"]) and path_denied("a/b/yarn.lock", ["**/yarn.lock"])
