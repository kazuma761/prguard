"""The /review pipeline (LangGraph):

build_context -> [security | quality | tests | docs | ci_fixer] -> aggregate -> check -> hitl_gate -> publish

Specialists run in parallel; how many actually hit the GPU at once is capped by
LLM_CONCURRENCY. The verdict and the CI gate are decided by plain code, never
by a model, so PR content cannot argue its way past them.
"""

from __future__ import annotations

import asyncio
import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from backend import context as ctxmod
from backend import reports
from backend.agents import aggregator, checker, ci_fixer, specialists
from backend.config import settings
from backend.events import bus
from backend.gateway import gateway
from backend.models import Denial, Event, Finding, Review, TaskContext, UpstreamError


class ReviewState(TypedDict, total=False):
    ctx: TaskContext
    options: ctxmod.Options
    context: dict[str, Any]
    findings_by_agent: Annotated[dict[str, list[Finding]], operator.or_]
    agent_errors: Annotated[dict[str, str], operator.or_]
    review: Review
    role: str  # only set on the per-specialist payload


def _span(ctx: TaskContext, source: str, kind: str, **payload: Any) -> None:
    bus.emit(Event(type=kind, review_id=ctx.review_id, source=source, payload=payload))


def _llm_label() -> str:
    return "MOCK LLM" if settings.llm_is_mock else settings.llm_model


async def build_context(state: ReviewState) -> dict[str, Any]:
    ctx = state["ctx"]
    _span(ctx, ctxmod.ORCHESTRATOR, "span.start", node="build_context")
    context = state["context"]
    if "files" not in context:
        context = await ctxmod.build(ctx, context["config"], gateway(), state["options"], want_checks=True)
    _span(ctx, ctxmod.ORCHESTRATOR, "span.end", node="build_context", files=len(context["files"]),
          note=context.get("diff_note", ""), checks=context["check_results"].get("source", ""))
    return {"context": context}


def fan_out(state: ReviewState) -> list[Send]:
    cfg = state["context"]["config"]
    sends = [Send("specialist", {**state, "role": role}) for role in cfg.review.specialists]
    if cfg.review.ci_fixer:
        sends.append(Send("ci_fixer", state))
    return sends or [Send("aggregate", state)]


async def _guarded(ctx: TaskContext, source: str, node: str, coro) -> tuple[list[Finding], dict[str, Any], str]:
    """Run one agent. Returns (findings, usage, error); an error never blocks the join."""
    _span(ctx, source, "span.start", node=node, llm=_llm_label())
    error = ""
    try:
        findings, usage = await asyncio.wait_for(coro, timeout=settings.node_timeout_s)
        note = usage.pop("note", "") if isinstance(usage, dict) else ""
    except asyncio.TimeoutError:
        findings, usage, note = [], {}, f"timed out after {settings.node_timeout_s:.0f}s"
        error = note
    except Exception as exc:
        findings, usage, note = [], {}, f"{type(exc).__name__}: {exc}"[:300]
        error = note
    for finding in findings:
        bus.emit(Event(type="finding", review_id=ctx.review_id, source=source, payload=finding.model_dump()))
    _span(ctx, source, "span.end", node=node, findings=len(findings), note=note, usage=usage)
    return findings, usage, error


async def specialist(state: ReviewState) -> dict[str, Any]:
    ctx, role = state["ctx"], state["role"]
    findings, _, error = await _guarded(ctx, specialists.agent_id(role), role,
                                        specialists.run_specialist(role, ctx, state["context"], gateway()))
    return {"findings_by_agent": {role: findings}, "agent_errors": {role: error} if error else {}}


async def ci_fixer_node(state: ReviewState) -> dict[str, Any]:
    ctx = state["ctx"]
    findings, _, error = await _guarded(ctx, ci_fixer.AGENT_ID, "ci", ci_fixer.run_ci_fixer(ctx, state["context"], gateway()))
    return {"findings_by_agent": {"ci": findings}, "agent_errors": {"ci": error} if error else {}}


async def aggregate(state: ReviewState) -> dict[str, Any]:
    ctx = state["ctx"]
    _span(ctx, aggregator.AGENT_ID, "span.start", node="aggregate")
    findings = aggregator.merge(state.get("findings_by_agent", {}))
    overall, verdict = aggregator.score(findings)
    notes = [n for n in [state["context"].get("diff_note", "")] + state["context"]["config"].warnings if n]
    errors = state.get("agent_errors") or {}
    notes += [f"{agent} reviewer failed: {err}" for agent, err in sorted(errors.items())]
    checks = state["context"]["check_results"]
    if checks.get("ran"):
        notes.append(f"checks: {checks.get('source')} exit codes {checks.get('exit_codes')}")
    review = Review(review_id=ctx.review_id, findings=findings, overall_confidence=overall, verdict=verdict, notes=notes)
    _span(ctx, aggregator.AGENT_ID, "span.end", node="aggregate", findings=len(findings), verdict=verdict)
    return {"review": review}


async def check(state: ReviewState) -> dict[str, Any]:
    ctx, review = state["ctx"], state["review"]
    _span(ctx, checker.AGENT_ID, "span.start", node="check")
    usage: dict[str, Any] = {}
    try:
        findings, notes, usage = await asyncio.wait_for(
            checker.check(review.findings, state["context"]["prompt_diff"]), timeout=settings.node_timeout_s)
    except asyncio.TimeoutError:
        findings, notes = review.findings, "independent checker timed out; findings are unverified"
    overall, verdict = aggregator.score(findings)
    review = review.model_copy(update={"findings": findings, "overall_confidence": overall, "verdict": verdict,
                                       "checker_notes": notes})
    _span(ctx, checker.AGENT_ID, "span.end", node="check", kept=len(findings), notes=notes, usage=usage)
    return {"review": review}


async def hitl_gate(state: ReviewState) -> dict[str, Any]:
    ctx, review = state["ctx"], state["review"]
    _span(ctx, "hitl-gate", "span.start", node="hitl_gate")
    if review.verdict == "NEEDS_HUMAN":
        bus.emit(Event(type="hitl.waiting", review_id=ctx.review_id, source="hitl-gate",
                       reason="critical finding, low confidence or reviewer manipulation; APPROVE stays locked until a human approves"))
    _span(ctx, "hitl-gate", "span.end", node="hitl_gate", verdict=review.verdict)
    return {}


def labels() -> list[str]:
    out = [f"reviewers: {_llm_label()}"]
    if settings.checker:
        out.append(f"checker: {settings.checker[2]}")
    if settings.upstream == "mock":
        out.append("GitHub: MOCK upstream")
    return out


async def publish(state: ReviewState) -> dict[str, Any]:
    """Upsert the persistent review comment; optionally also submit REQUEST_CHANGES."""
    ctx, review, cfg = state["ctx"], state["review"], state["context"]["config"]
    _span(ctx, aggregator.AGENT_ID, "span.start", node="publish")
    gate = reports.gate(review, cfg.gate)
    blocked = sum(1 for e in bus.history if e.review_id == ctx.review_id and e.type == "decision" and e.decision == "DENY")
    review = review.model_copy(update={"summary_markdown": aggregator.summary_markdown(review, ctx.head_sha, blocked, labels(), gate)})

    if state["options"].publish:
        gw = gateway()
        try:
            res = await gw.call(aggregator.AGENT_ID, ctx, "upsert_issue_comment",
                                {"repo": ctx.repo_full_name, "pull_number": ctx.pr_number, "marker": "review",
                                 "body": review.summary_markdown})
            if not isinstance(res, Denial) and isinstance(res.data, dict):
                review = review.model_copy(update={"posted_url": res.data.get("html_url", "")})
                bus.emit(Event(type="review.posted", review_id=ctx.review_id, source=aggregator.AGENT_ID,
                               payload={"url": review.posted_url, "event": "COMMENT", "updated": res.data.get("updated")}))
        except UpstreamError:
            pass
        if cfg.review.request_changes and review.verdict == "REQUEST_CHANGES":
            try:
                await gw.call(aggregator.AGENT_ID, ctx, "create_pull_request_review",
                              {"repo": ctx.repo_full_name, "pull_number": ctx.pr_number, "event": "REQUEST_CHANGES",
                               "body": f"PRGuard found blocking issues. Details: {review.posted_url or 'see the PRGuard comment'}"})
            except UpstreamError:
                pass  # e.g. GitHub refuses REQUEST_CHANGES on your own PR

    bus.emit(Event(type="review.ready", review_id=ctx.review_id, source=aggregator.AGENT_ID, payload=review.model_dump()))
    _span(ctx, aggregator.AGENT_ID, "span.end", node="publish", posted=bool(review.posted_url))
    return {"review": review}


def build_graph():
    graph = StateGraph(ReviewState)
    graph.add_node("build_context", build_context)
    graph.add_node("specialist", specialist)
    graph.add_node("ci_fixer", ci_fixer_node)
    graph.add_node("aggregate", aggregate)
    graph.add_node("check", check)
    graph.add_node("hitl_gate", hitl_gate)
    graph.add_node("publish", publish)
    graph.add_edge(START, "build_context")
    graph.add_conditional_edges("build_context", fan_out, ["specialist", "ci_fixer", "aggregate"])
    graph.add_edge("specialist", "aggregate")
    graph.add_edge("ci_fixer", "aggregate")
    graph.add_edge("aggregate", "check")
    graph.add_edge("check", "hitl_gate")
    graph.add_edge("hitl_gate", "publish")
    graph.add_edge("publish", END)
    return graph.compile()


pipeline = build_graph()


async def run_review(ctx: TaskContext, context: dict[str, Any], options: ctxmod.Options) -> tuple[Review, dict[str, str]]:
    """Return (review, errors by agent). `context` must hold at least {"config": RepoConfig}."""
    state = await pipeline.ainvoke({"ctx": ctx, "options": options, "context": context,
                                    "findings_by_agent": {}, "agent_errors": {}})
    return state["review"], state.get("agent_errors") or {}
