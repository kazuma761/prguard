"""Shared data contracts: TaskContext, Finding, Review, Event, gateway results."""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Enforcer(str, Enum):
    POLICY = "POLICY"


class TaskContext(BaseModel):
    """Trusted description of one job (a review, describe, improve or ask).

    Built only from a verified webhook, the CI event file or an operator-supplied
    PR, never from LLM output or PR content. `human_approved` is flipped only by
    the /approve endpoint.
    """

    review_id: str
    repo_full_name: str
    pr_number: int
    head_sha: str
    base_ref: str = "main"
    sender: str = "unknown"
    command: str = "review"
    human_approved: bool = False


Severity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
AgentType = Literal["security", "quality", "tests", "docs", "ci"]
SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}


class Finding(BaseModel):
    agent_type: AgentType
    severity: Severity
    category: str
    summary: str
    file_path: str = ""
    line_start: int = 0
    line_end: int = 0
    suggestion: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = ""
    # Filled in after the specialists run.
    agreed_by: list[str] = Field(default_factory=list)
    checker_status: Literal["CONFIRMED", "DOWNGRADED", "REJECTED", "UNVERIFIED"] = "UNVERIFIED"
    checker_reason: str = ""


Verdict = Literal["COMMENT", "REQUEST_CHANGES", "NEEDS_HUMAN"]


class Review(BaseModel):
    review_id: str
    findings: list[Finding] = Field(default_factory=list)
    overall_confidence: float = 0.0
    verdict: Verdict = "COMMENT"
    checker_notes: str = ""
    summary_markdown: str = ""
    posted_url: str = ""
    notes: list[str] = Field(default_factory=list)


class CodeSuggestion(BaseModel):
    """One /improve suggestion, posted as an inline GitHub suggestion block."""

    file_path: str
    line_start: int
    line_end: int
    existing_code: str = ""
    improved_code: str
    summary: str
    label: str = "maintainability"
    importance: int = Field(default=5, ge=1, le=10)


class Description(BaseModel):
    """Output of /describe."""

    title: str = ""
    change_type: list[str] = Field(default_factory=list)
    summary: str = ""
    walkthrough: list[dict[str, str]] = Field(default_factory=list)
    risks: str = ""


class CommandResult(BaseModel):
    """What one command produced, for the CLI, reports and the UI."""

    command: str
    review_id: str
    repo: str
    pr_number: int
    head_sha: str
    ok: bool = True
    error: str = ""
    review: Review | None = None
    description: Description | None = None
    suggestions: list[CodeSuggestion] = Field(default_factory=list)
    answer: str = ""
    posted_urls: list[str] = Field(default_factory=list)
    gate: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    duration_s: float = 0.0


EventType = Literal[
    "span.start",
    "span.end",
    "tool.call",
    "decision",
    "finding",
    "sandbox",
    "review.posted",
    "hitl.waiting",
    "hitl.approved",
    "review.ready",
    "command.done",
    "error",
]


class Event(BaseModel):
    ts: float = Field(default_factory=time.time)
    review_id: str = ""
    source: str = ""
    type: EventType
    tool: str | None = None
    args_redacted: dict[str, Any] | None = None
    decision: Literal["ALLOW", "DENY"] | None = None
    reason: str | None = None
    enforcer: Enforcer | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: Literal[True] = True
    tool: str
    data: Any = None


class Denial(BaseModel):
    """A call the access-control layer refused. Falsy, so `if not res:` works."""

    ok: Literal[False] = False
    tool: str
    reason: str
    enforcer: Enforcer = Enforcer.POLICY
    policy: str = ""

    def __bool__(self) -> bool:
        return False


class UpstreamError(Exception):
    """The call was allowed but GitHub (or the mock) failed it."""
