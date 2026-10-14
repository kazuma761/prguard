"""Turn GitHub events into PRGuard jobs.

Used by the webhook server and by `prguard run --event-path $GITHUB_EVENT_PATH`
in GitHub Actions. Handles:

- pull_request (opened / reopened / ready_for_review -> [auto] on_open,
  synchronize -> [auto] on_push), drafts skipped;
- issue_comment on a PR whose first line is /review, /describe, /improve or
  /ask <question>, from an allowed author association (default: owners,
  members, collaborators). Bots are ignored, so PRGuard never triggers itself.

The TaskContext is built from the event and GitHub's answer about the PR,
never from comment text or PR content.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.context import ORCHESTRATOR, load_config
from backend.gateway.base import ToolGateway
from backend.models import Denial, TaskContext, UpstreamError
from backend.repo_config import COMMANDS, RepoConfig

_PR_URL = re.compile(r"^https?://[^/\s]+/([\w.-]+/[\w.-]+)/pull/(\d+)/?$")
_COMMAND = re.compile(r"^\s*/(review|describe|improve|ask)\b[ \t]*(.*)$", re.IGNORECASE | re.DOTALL)
OPEN_ACTIONS = {"opened", "reopened", "ready_for_review"}
PUSH_ACTIONS = {"synchronize"}


@dataclass
class Job:
    ctx: TaskContext
    commands: list[str]
    cfg: RepoConfig
    question: str = ""
    trigger: str = ""
    notes: list[str] = field(default_factory=list)


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check X-Hub-Signature-256. With no secret configured, nothing verifies."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def new_review_id() -> str:
    return uuid.uuid4().hex[:12]


def parse_pr_url(url: str) -> tuple[str, int] | None:
    match = _PR_URL.match(url.strip())
    return (match.group(1), int(match.group(2))) if match else None


def parse_command(body: str) -> tuple[str, str] | None:
    """('review'|'describe'|'improve'|'ask', rest) from the first non-empty line of a comment."""
    text = (body or "").lstrip()
    match = _COMMAND.match(text)
    if not match:
        return None
    command, rest = match.group(1).lower(), match.group(2).strip()
    return (command, rest) if command in COMMANDS else None


async def context_for_pr(gateway: ToolGateway, repo: str, number: int, sender: str = "unknown") -> TaskContext:
    """Look the PR up through the gateway and build a trusted context for its current head commit."""
    probe = TaskContext(review_id=new_review_id(), repo_full_name=repo, pr_number=number, head_sha="", sender=sender)
    res = await gateway.call(ORCHESTRATOR, probe, "get_pull_request", {"repo": repo, "pull_number": number})
    if isinstance(res, Denial):
        raise UpstreamError(f"access policy refused the pull request lookup: {res.reason}")
    pr = res.data if isinstance(res.data, dict) else {}
    if "head" not in pr:
        raise UpstreamError("could not load the pull request")
    return probe.model_copy(update={"head_sha": pr["head"]["sha"], "base_ref": pr.get("base", {}).get("ref", "main"),
                                    "sender": sender if sender != "unknown" else pr.get("user", {}).get("login", "unknown")})


async def job_from_event(event: str, payload: dict[str, Any], gateway: ToolGateway) -> tuple[Job | None, str]:
    """Return (job, reason). job is None when the event should be ignored; reason says why."""
    repo = (payload.get("repository") or {}).get("full_name", "")
    if event == "pull_request":
        action = payload.get("action")
        pr = payload.get("pull_request") or {}
        if action not in OPEN_ACTIONS | PUSH_ACTIONS:
            return None, f"pull_request action {action!r} is not reviewed"
        if pr.get("draft"):
            return None, "draft pull request"
        ctx = TaskContext(review_id=new_review_id(), repo_full_name=repo, pr_number=int(pr["number"]),
                          head_sha=pr["head"]["sha"], base_ref=pr["base"]["ref"], sender=pr["user"]["login"])
        cfg = await load_config(ctx, gateway)
        trigger = "open" if action in OPEN_ACTIONS else "push"
        commands = cfg.auto.on_open if trigger == "open" else cfg.auto.on_push
        if not commands:
            return None, f"[auto] on_{trigger} is empty in .prguard.toml"
        return Job(ctx=ctx, commands=list(commands), cfg=cfg, trigger=f"pull_request.{action}"), "ok"

    if event == "issue_comment":
        if payload.get("action") != "created":
            return None, "only new comments trigger commands"
        issue, comment = payload.get("issue") or {}, payload.get("comment") or {}
        if "pull_request" not in issue:
            return None, "comment is on an issue, not a pull request"
        user = comment.get("user") or {}
        if user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]"):
            return None, "comments by bots are ignored"
        parsed = parse_command(comment.get("body", ""))
        if not parsed:
            return None, "comment is not a PRGuard command"
        command, rest = parsed
        ctx = await context_for_pr(gateway, repo, int(issue["number"]), sender=user.get("login", "unknown"))
        cfg = await load_config(ctx, gateway)
        association = str(comment.get("author_association", "NONE")).upper()
        allowed = {a.upper() for a in cfg.commands.allowed_associations}
        if association not in allowed:
            return None, f"{user.get('login')} ({association}) may not run PRGuard commands (allowed: {sorted(allowed)})"
        if command == "ask" and not rest:
            return None, "/ask needs a question"
        return Job(ctx=ctx, commands=[command], cfg=cfg, question=rest if command == "ask" else "",
                   trigger=f"comment /{command} by {user.get('login')}"), "ok"

    return None, f"event {event!r} is not handled"
