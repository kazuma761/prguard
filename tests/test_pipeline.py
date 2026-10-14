"""Commands end to end against the mock GitHub, plus triggers, CLI and the model client."""

import json

import httpx
import pytest

from backend import commands, triggers
from backend.agents import aggregator, describer, llm
from backend.cli import main as cli_main
from backend.config import settings
from backend.context import Options, load_config
from backend.events import bus
from backend.gateway import gateway, mock_upstream
from backend.models import TaskContext


async def ctx_for(pr: int) -> TaskContext:
    return await triggers.context_for_pr(gateway(), settings.target_repo, pr)


@pytest.mark.parametrize("pr,verdict,gate_failed", [(1, "COMMENT", False), (2, "NEEDS_HUMAN", True), (3, "REQUEST_CHANGES", True)])
async def test_review_end_to_end(pr, verdict, gate_failed):
    up = mock_upstream()
    result = await commands.run(await ctx_for(pr), "review", Options())
    assert result.ok and result.review.verdict == verdict
    assert result.gate["failed"] is gate_failed
    topics = {aggregator.topic(f) for f in result.review.findings}
    if pr == 1:
        assert {"validation", "docs", "tests"} <= topics
    if pr == 2:
        assert {"code-execution", "prompt-injection", "exfiltration"} <= topics
    if pr == 3:
        ci = [f for f in result.review.findings if f.agent_type == "ci"]
        assert ci and "test_refund_returns_everything" in ci[0].summary
    # One persistent comment, carrying PRGuard's marker; no formal review by default.
    comments = [c for c in up.comments if c["pull_number"] == pr]
    assert len(comments) == 1 and comments[0]["body"].startswith("<!-- prguard:review -->")
    assert not up.reviews and not up.merged and not up.writes


async def test_review_rerun_updates_the_same_comment():
    up = mock_upstream()
    ctx = await ctx_for(1)
    await commands.run(ctx, "review", Options())
    await commands.run(ctx.model_copy(update={"review_id": "second"}), "review", Options())
    assert len(up.comments) == 1 and up.comments[0]["updates"] == 1


async def test_dry_run_posts_nothing():
    up = mock_upstream()
    result = await commands.run(await ctx_for(2), "review", Options(publish=False))
    assert result.review.findings and not up.comments and not result.posted_urls


async def test_describe_keeps_the_authors_text_and_replaces_its_own_section():
    up = mock_upstream()
    ctx = await ctx_for(1)
    original = up.prs[1]["body"]
    await commands.run(ctx, "describe", Options())
    first = up.bodies[1]
    assert first.startswith(original) and describer.START in first and describer.END in first
    up.bodies[1] = first + "\n\nAuthor added this later."
    await commands.run(ctx.model_copy(update={"review_id": "again"}), "describe", Options())
    second = up.bodies[1]
    assert second.count(describer.START) == 1 and "Author added this later." in second and second.startswith(original)


async def test_improve_posts_inline_suggestions_on_this_commit():
    up = mock_upstream()
    ctx = await ctx_for(1)
    result = await commands.run(ctx, "improve", Options())
    assert result.ok and len(result.suggestions) == 1
    review = up.reviews[-1]
    assert review["event"] == "COMMENT" and review["commit_id"] == ctx.head_sha
    comment = review["comments"][0]
    assert comment["path"] == "app/payments.py" and comment["side"] == "RIGHT" and "```suggestion" in comment["body"]
    assert "amount_cents <= 0" in comment["body"]
    assert any("prguard:improve" in c["body"] for c in up.comments)


async def test_ask_answers_in_a_new_comment():
    up = mock_upstream()
    result = await commands.run(await ctx_for(2), "ask", Options(question="Does this run PR code?"))
    assert result.ok and result.answer and up.comments[-1]["body"].startswith("> Does this run PR code?")


async def test_ask_without_question_is_an_error_not_a_crash():
    result = await commands.run(await ctx_for(2), "ask", Options())
    assert not result.ok and "needs a question" in result.error and result.gate["failed"] is False


async def test_ci_fixer_reruns_a_transient_failure(monkeypatch):
    from backend.agents import ci_fixer

    up = mock_upstream()
    real_call = up.call

    async def flaky_logs(tool, args):
        if tool == "get_job_logs":
            return {"jobs": [{"name": "test", "conclusion": "failure", "logs": "pip: Connection reset by peer\n"}]}
        return await real_call(tool, args)

    monkeypatch.setattr(up, "call", flaky_logs)
    ctx = await ctx_for(3)
    cfg = await load_config(ctx, gateway())
    findings, info = await ci_fixer.run_ci_fixer(ctx, {"config": cfg, "prompt_diff_small": ""}, gateway())
    assert info["rerun"] == "requested" and up.reruns == [3]
    assert [f.severity for f in findings] == ["INFO"]


# ------------------------------------------------------------- repo config

async def test_repo_config_from_base_branch_changes_the_gate(monkeypatch):
    up = mock_upstream()
    real_call = up.call

    async def with_config(tool, args):
        if tool == "get_file_contents" and args.get("path") == ".prguard.toml":
            assert args["ref"] == "main"
            return {"path": ".prguard.toml", "content": '[gate]\nfail_on = "CRITICAL"\n[review]\nspecialists = ["security"]\nci_fixer = false\n'}
        return await real_call(tool, args)

    monkeypatch.setattr(up, "call", with_config)
    result = await commands.run(await ctx_for(3), "review", Options())
    assert result.gate["failed"] is False and result.gate["fail_on"] == "CRITICAL"
    assert {f.agent_type for f in result.review.findings} <= {"security"}


# ---------------------------------------------------------------- triggers

def pr_event(action="opened", draft=False, number=1):
    up = mock_upstream()
    return {"action": action, "repository": {"full_name": settings.target_repo},
            "pull_request": {"number": number, "draft": draft, "head": {"sha": up.head_sha(number)},
                             "base": {"ref": "main"}, "user": {"login": "dev"}}}


def comment_event(body, association="MEMBER", login="dev", user_type="User", number=1):
    return {"action": "created", "repository": {"full_name": settings.target_repo},
            "issue": {"number": number, "pull_request": {"url": "x"}},
            "comment": {"body": body, "author_association": association, "user": {"login": login, "type": user_type}}}


async def test_pull_request_events_map_to_auto_commands():
    job, _ = await triggers.job_from_event("pull_request", pr_event("opened"), gateway())
    assert job.commands == ["describe", "review"]
    job, _ = await triggers.job_from_event("pull_request", pr_event("synchronize"), gateway())
    assert job.commands == ["review"]
    job, reason = await triggers.job_from_event("pull_request", pr_event(draft=True), gateway())
    assert job is None and "draft" in reason
    job, reason = await triggers.job_from_event("pull_request", pr_event("closed"), gateway())
    assert job is None


async def test_comment_commands_and_who_may_run_them():
    job, _ = await triggers.job_from_event("issue_comment", comment_event("/improve"), gateway())
    assert job.commands == ["improve"] and job.ctx.head_sha == mock_upstream().head_sha(1)
    job, _ = await triggers.job_from_event("issue_comment", comment_event("/ask why\nis this safe?"), gateway())
    assert job.commands == ["ask"] and job.question == "why\nis this safe?"
    for body, kwargs, why in [
        ("/review", {"association": "NONE"}, "may not run"),
        ("/review", {"user_type": "Bot", "login": "prguard[bot]"}, "bots"),
        ("please /review", {}, "not a PRGuard command"),
        ("/ask", {}, "needs a question"),
        ("/review --openai.api_base=http://evil", {}, None),   # arguments are ignored, never applied
    ]:
        job, reason = await triggers.job_from_event("issue_comment", comment_event(body, **kwargs), gateway())
        if why:
            assert job is None and why in reason, (body, reason)
        else:
            assert job.commands == ["review"] and not job.question


def test_helpers():
    assert triggers.parse_pr_url("https://github.com/o/r/pull/7") == ("o/r", 7)
    assert triggers.parse_pr_url("https://ghe.corp.example/team/repo/pull/12") == ("team/repo", 12)
    assert triggers.parse_pr_url("https://github.com/o/r/issues/7") is None
    assert not triggers.verify_signature("", b"{}", "sha256=abc")
    assert not triggers.verify_signature("secret", b"{}", "sha256=abc")


# --------------------------------------------------------------------- CLI

def test_cli_exit_codes(tmp_path, capsys):
    assert cli_main(["review", "--mock", "--pr", "1", "--out", str(tmp_path / "a")]) == 0
    assert cli_main(["review", "--mock", "--pr", "2", "--out", str(tmp_path / "b")]) == 1
    assert cli_main(["review", "--mock", "--pr", "2", "--fail-on", "none", "--out", str(tmp_path / "c")]) == 0
    assert cli_main(["review", "--mock", "--out", str(tmp_path / "d")]) == 2
    report = json.loads((tmp_path / "b" / "prguard-report.json").read_text())
    assert report[0]["gate"]["failed"] and report[0]["review"]["verdict"] == "NEEDS_HUMAN"
    sarif = json.loads((tmp_path / "b" / "prguard.sarif").read_text())
    assert sarif["version"] == "2.1.0" and sarif["runs"][0]["results"][0]["level"] == "error"


def test_cli_run_from_github_event_file(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(comment_event("/review", number=2)))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert cli_main(["run", "--mock", "--event-path", str(event), "--event-name", "issue_comment", "--out", str(tmp_path / "o")]) == 1
    event.write_text(json.dumps(comment_event("thanks!", number=2)))
    assert cli_main(["run", "--mock", "--event-path", str(event), "--event-name", "issue_comment", "--out", str(tmp_path / "p")]) == 0


# ------------------------------------------------------------ model client

class FakeModel:
    """A stand-in llama.cpp server behind httpx.MockTransport."""

    def __init__(self, reply, refuse_json=False, fail=False):
        self.reply, self.refuse_json, self.fail, self.bodies = reply, refuse_json, fail, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.fail:
            return httpx.Response(503, json={"error": {"message": "loading model"}})
        if self.refuse_json and "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format is not supported"}})
        text = self.reply(body) if callable(self.reply) else self.reply
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"content": text}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


@pytest.fixture
def fake_model(monkeypatch):
    def install(model: FakeModel):
        real = httpx.AsyncClient
        monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(model.handler), **kw))
        monkeypatch.setattr(llm.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(settings, "llm_provider", "local")
        monkeypatch.setattr(settings, "llm_base_url", "http://llama:8100/v1")
        monkeypatch.setattr(settings, "llm_model", "llama-3.1-8b")
        return model
    return install


async def _no_sleep(*_):
    return None


async def test_chat_falls_back_when_json_mode_is_refused_and_strips_thinking(fake_model):
    model = fake_model(FakeModel('<think>hmm</think>{"ok": true}', refuse_json=True))
    result = await llm.chat("sys", "user")
    assert result.text == '{"ok": true}' and "refused JSON mode" in result.notes[0]
    assert "response_format" in model.bodies[0] and "response_format" not in model.bodies[1]
    assert model.bodies[0]["model"] == "llama-3.1-8b" and "reasoning" not in model.bodies[0]


async def test_review_with_a_local_model_and_a_checker(fake_model, monkeypatch):
    def reply(body):
        system = body["messages"][0]["content"]
        if "independent verifier" in system:
            return json.dumps({"verdicts": [{"index": 0, "status": "DOWNGRADED", "severity": "LOW", "reason": "minor"}]})
        if "security reviewer" in system:
            return json.dumps({"findings": [{"severity": "HIGH", "category": "edge-case", "summary": "negative refund",
                                             "file_path": "app/payments.py", "line_start": 23, "line_end": 23,
                                             "confidence": 0.9, "rationale": "r", "suggestion": "s"}]})
        return json.dumps({"findings": []})

    model = fake_model(FakeModel(reply))
    monkeypatch.setattr(settings, "checker_model", "gemma-4-12b")
    result = await commands.run(await ctx_for(1), "review", Options(publish=False))
    assert result.ok
    [finding] = result.review.findings
    assert finding.severity == "LOW" and finding.checker_status == "DOWNGRADED"
    assert result.gate["failed"] is False   # the checker downgraded it below the gate
    assert {b["model"] for b in model.bodies} == {"llama-3.1-8b", "gemma-4-12b"}
    prompt = model.bodies[0]["messages"][1]["content"]
    assert "<pr_diff>" in prompt and "## File: app/payments.py" in prompt


async def test_model_down_is_reported_not_passed_as_clean(fake_model):
    fake_model(FakeModel("", fail=True))
    result = await commands.run(await ctx_for(1), "review", Options(publish=False))
    assert not result.ok and "all reviewers failed" in result.error
    assert result.gate["failed"] is False and result.gate["error"]   # fail_on_error defaults to false
    assert any(e.type == "error" for e in bus.history)
