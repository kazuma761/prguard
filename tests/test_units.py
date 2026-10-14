"""Pure pieces: diff rendering and budgets, suggestion validation, repo config, the gate, merging."""

from backend import diffs, repo_config, reports
from backend.agents import aggregator, improver
from backend.events import redact
from backend.models import Finding, Review

PATCH = """@@ -18,3 +18,8 @@ def refund(customer_id: str) -> int:
     return _BALANCES.pop(customer_id, 0)


+def refund_partial(customer_id: str, amount_cents: int) -> int:
+    balance = _BALANCES.get(customer_id, 0)
+    if amount_cents > balance:
-    old = 1
+        raise PaymentError("too much")
+    return balance - amount_cents
"""


def test_diff_lines_are_numbered_with_new_file_lines():
    rendered = diffs.render_file("app/payments.py", PATCH)
    assert "   21 + def refund_partial" in rendered and "      -     old = 1" in rendered
    assert diffs.added_lines(PATCH) == {21, 22, 23, 24, 25}
    assert diffs.commentable_lines(PATCH) == {18, 19, 20, 21, 22, 23, 24, 25}


def test_prompt_budget_skips_generated_files_and_lists_what_did_not_fit():
    files = [{"filename": "package-lock.json", "status": "modified", "patch": PATCH * 50},
             {"filename": "app/big.py", "status": "modified", "patch": PATCH * 200, "additions": 1000},
             {"filename": "app/small.py", "status": "modified", "patch": PATCH, "additions": 5},
             {"filename": "README.md", "status": "modified", "patch": PATCH, "additions": 5}]
    out = diffs.build_prompt_diff(files, repo_config.DEFAULT_IGNORE, budget_tokens=600)
    assert out.ignored == ["package-lock.json"]
    assert "app/big.py" in out.included and "app/big.py" in out.clipped   # most-changed source first, clipped
    assert "README.md" in out.omitted and "left out" in out.note()
    assert diffs.est_tokens(out.text) <= 700


def test_split_unified_diff_fallback():
    diff = "diff --git a/x.py b/x.py\nnew file mode 100644\n--- /dev/null\n+++ b/x.py\n@@ -0,0 +1,2 @@\n+a\n+b\n"
    [f] = diffs.split_unified_diff(diff)
    assert f["filename"] == "x.py" and f["status"] == "added" and diffs.added_lines(f["patch"]) == {1, 2}


def test_improve_validation_moves_or_drops_bad_suggestions():
    head = "\n".join(f"line {n}" for n in range(1, 18)) + "\n" + "\n".join([
        "    return _BALANCES.pop(customer_id, 0)", "", "",
        "def refund_partial(customer_id: str, amount_cents: int) -> int:",
        "    balance = _BALANCES.get(customer_id, 0)",
        "    if amount_cents > balance:",
        '        raise PaymentError("too much")',
        "    return balance - amount_cents"])
    files = [{"filename": "app/payments.py", "patch": PATCH}]
    good = {"file_path": "app/payments.py", "line_start": 23, "line_end": 23, "summary": "reject negatives",
            "existing_code": "    if amount_cents > balance:", "improved_code": "    if amount_cents <= 0 or amount_cents > balance:",
            "importance": 8, "label": "bug"}
    wrong_line = {**good, "line_start": 3, "line_end": 3}                              # relocated to 23
    numbered = {**good, "existing_code": "   23 +     if amount_cents > balance:"}     # copied prompt numbers
    context_only = {**good, "line_start": 18, "line_end": 18, "existing_code": "    return _BALANCES.pop(customer_id, 0)",
                    "improved_code": "    return _BALANCES.pop(customer_id)"}         # not added by this PR
    other_file = {**good, "file_path": "app/other.py"}
    low = {**good, "line_start": 24, "line_end": 24, "existing_code": '        raise PaymentError("too much")',
           "improved_code": '        raise PaymentError("refund exceeds balance")', "importance": 2}
    kept, dropped = improver.validate([wrong_line, numbered, context_only, other_file, low], files,
                                      {"app/payments.py": head}, min_importance=4, limit=5)
    assert [(s.line_start, s.label) for s in kept] == [(23, "bug")]   # duplicates of one range collapse to one
    assert len(dropped) == 3


def test_repo_config_parsing():
    cfg = repo_config.parse("""
[gate]
fail_on = "medium"
min_confidence = 0.8
[review]
specialists = ["security", "nonsense"]
bogus = 1
[auto]
on_open = ["describe", "review", "ask"]
[ignore]
paths = ["generated/**"]
""")
    assert cfg.gate.fail_on == "MEDIUM" and cfg.gate.min_confidence == 0.8
    assert cfg.review.specialists == ["security"] and cfg.auto.on_open == ["describe", "review"]
    assert "generated/**" in cfg.ignore and "**/*.lock" in cfg.ignore
    assert any("bogus" in w for w in cfg.warnings) and any("nonsense" in w for w in cfg.warnings)
    broken = repo_config.parse("[gate\nfail_on=")
    assert broken.source == "defaults" and broken.warnings


def f(severity, confidence=0.9, status="CONFIRMED", category="bug", agent="quality"):
    return Finding(agent_type=agent, severity=severity, category=category, summary="s", file_path="a.py",
                   line_start=1, line_end=1, confidence=confidence, checker_status=status)


def test_gate():
    cfg = repo_config.GateCfg()
    assert reports.gate(Review(review_id="r", findings=[f("MEDIUM")]), cfg)["failed"] is False
    assert reports.gate(Review(review_id="r", findings=[f("HIGH")]), cfg)["failed"] is True
    assert reports.gate(Review(review_id="r", findings=[f("HIGH", confidence=0.4)]), cfg)["failed"] is False
    cfg.fail_on = "NONE"
    assert reports.gate(Review(review_id="r", findings=[f("CRITICAL")]), cfg)["failed"] is False
    cfg.fail_on, cfg.fail_on_needs_human = "CRITICAL", True
    assert reports.gate(Review(review_id="r", findings=[], verdict="NEEDS_HUMAN"), cfg)["failed"] is True


def test_merge_groups_by_topic_not_by_category_name():
    def g(agent, severity, category, summary, start, end, conf=0.9):
        return Finding(agent_type=agent, severity=severity, category=category, summary=summary,
                       file_path="app/payments.py", line_start=start, line_end=end, confidence=conf)

    merged = aggregator.merge({
        "security": [g("security", "HIGH", "logic-error", "refund_partial allows negative amounts", 23, 28)],
        "quality": [g("quality", "MEDIUM", "input-validation", "does not validate non-positive amounts", 23, 28),
                    g("quality", "LOW", "documentation", "Missing docstring for refund_partial", 23, 23)],
        "docs": [g("docs", "LOW", "missing-docstring", "refund_partial lacks a docstring", 23, 28, 0.97)],
        "tests": [g("tests", "HIGH", "prompt-injection", "Embedded instruction attempts to bypass review", 19, 23)],
    })
    assert [(aggregator.topic(m), m.severity, m.agreed_by) for m in merged] == [
        ("prompt-injection", "HIGH", ["tests"]),
        ("validation", "HIGH", ["quality", "security"]),
        ("docs", "LOW", ["docs", "quality"]),
    ]
    assert aggregator.score(merged)[1] == "NEEDS_HUMAN"   # an injection attempt always goes to a human
    assert aggregator.score(merged[1:])[1] == "REQUEST_CHANGES"


def test_redaction():
    assert "ghp_" + "a" * 30 not in redact({"t": "ghp_" + "a" * 30})["t"]
    assert len(redact("x" * 500)) < 260


async def test_github_client_request_shapes(monkeypatch):
    """The REST client against a fake GitHub: persistent comments, inline reviews, PR body."""
    import json as _json

    import httpx

    from backend.gateway import github as gh

    calls = []
    comments = [{"id": 1, "body": "<!-- prguard:review -->\nold", "user": {"login": "mallory"}, "html_url": "u1"},
                {"id": 2, "body": "<!-- prguard:review -->\nold", "user": {"login": "github-actions[bot]"}, "html_url": "u2"}]

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/user":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if request.method == "GET" and request.url.path.endswith("/issues/5/comments"):
            return httpx.Response(200, json=comments)
        if request.method == "PATCH" and "/issues/comments/" in request.url.path:
            return httpx.Response(200, json={"id": 2, "html_url": "u2"})
        if request.url.path.endswith("/pulls/5/reviews"):
            return httpx.Response(200, json={"id": 9, "state": "COMMENTED", "html_url": "r9"})
        if request.method == "PATCH" and request.url.path.endswith("/pulls/5"):
            return httpx.Response(200, json={"number": 5, "html_url": "p5"})
        return httpx.Response(404, json={"message": "Not Found"})

    real = httpx.AsyncClient
    monkeypatch.setattr(gh.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    client = gh.GitHubUpstream(token="t", api_url="https://api.example")

    res = await client.call("upsert_issue_comment", {"repo": "o/r", "pull_number": 5, "marker": "review", "body": "new"})
    assert res == {"id": 2, "html_url": "u2", "updated": True}          # its own comment, never mallory's
    assert ("PATCH", "/repos/o/r/issues/comments/2", {"body": "<!-- prguard:review -->\nnew"}) in calls

    await client.call("create_pull_request_review", {"repo": "o/r", "pull_number": 5, "event": "COMMENT", "commit_id": "abc",
                                                     "body": "b", "comments": [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "x"}]})
    review_call = next(c for c in calls if c[1].endswith("/reviews"))
    assert review_call[2] == {"event": "COMMENT", "body": "b", "commit_id": "abc",
                              "comments": [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "x"}]}

    await client.call("update_pull_request_body", {"repo": "o/r", "pull_number": 5, "body": "desc", "title": "ignored"})
    assert ("PATCH", "/repos/o/r/pulls/5", {"body": "desc"}) in calls     # only the body is ever sent

    import pytest as _pytest

    from backend.models import UpstreamError
    for tool in ("merge_pull_request", "push_files", "create_or_update_file", "delete_file", "update_pull_request"):
        with _pytest.raises(UpstreamError):
            await client.call(tool, {"repo": "o/r", "pull_number": 5})
