"""Aggregator: merge, dedup, score, decide the verdict, write the summary.

Done in plain code rather than by an LLM so the verdict is deterministic and
cannot be argued with by PR content.
"""

from __future__ import annotations

import re

from backend.config import settings
from backend.models import Finding, Review

AGENT_ID = "prguard-aggregator"
_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}


# Different reviewers name the same problem differently ("network-call",
# "information-leakage", "environment-leak"), so findings are grouped by topic.
# First match wins; checked against the category, then the summary.
_TOPICS: list[tuple[str, tuple[str, ...]]] = [
    ("prompt-injection", ("injection", "manipulat", "ai reviewer")),
    ("ci-failure", ("ci-fail", "ci fail", "failing test", "test failure")),
    ("docs", ("docstring", "documentation", "undocumented", "missing-doc")),
    ("tests", ("missing-test", "missing test", "no test", "untested", "unit test", "test coverage", "lacks test")),
    ("code-execution", ("code-exec", "code exec", "untrusted", "init-hook", "load-plugin", "ci-config", "linter", "pylint config")),
    ("exfiltration", ("leak", "exfil", "environment variable", "network", "outbound", "http request")),
    ("division-by-zero", ("division", "divide by zero", "zerodivision")),
    ("concurrency", ("thread", "race condition", "concurren")),
    ("validation", ("validat", "negative", "non-positive", "edge", "logic", "bounds", "range")),
]


def _plain(text: str) -> str:
    """Lower-case with every kind of hyphen or dash turned into '-'."""
    return re.sub(r"[\u2010-\u2015\u2212]", "-", text.lower())


def topic(finding: Finding) -> str:
    for text in (_plain(finding.category), _plain(finding.summary)):
        for name, keywords in _TOPICS:
            if any(keyword in text for keyword in keywords):
                return name
    return _plain(finding.category)


def _overlaps(a: Finding, b: Finding) -> bool:
    """Same file, overlapping lines, same topic."""
    return (a.file_path == b.file_path and topic(a) == topic(b)
            and a.line_start <= max(b.line_end, b.line_start) and b.line_start <= max(a.line_end, a.line_start))


def merge(findings_by_agent: dict[str, list[Finding]]) -> list[Finding]:
    """Deduplicate across agents: one finding per file, line range and topic,
    keeping the most severe (then most confident) and recording who agreed."""
    merged: list[Finding] = []
    for agent, findings in sorted(findings_by_agent.items()):
        for finding in findings:
            twin = next((m for m in merged if _overlaps(m, finding)), None)
            if twin is None:
                merged.append(finding.model_copy(update={"agreed_by": [agent]}))
                continue
            keep = max(twin, finding, key=lambda f: (_RANK[f.severity], f.confidence))
            merged[merged.index(twin)] = keep.model_copy(update={"agreed_by": sorted({*twin.agreed_by, agent})})
    merged.sort(key=lambda f: (-_RANK[f.severity], -f.confidence, f.file_path, f.line_start))
    return merged


def score(findings: list[Finding]) -> tuple[float, str]:
    """Confidence-weighted mean confidence, and the verdict it implies.

    NEEDS_HUMAN on any CRITICAL finding, on low overall confidence, or when the
    PR tries to instruct the reviewers (prompt injection): that last one is never
    left to an automatic verdict.
    """
    total = sum(f.confidence for f in findings)
    overall = sum(f.confidence * f.confidence for f in findings) / total if total else 1.0
    manipulated = any(topic(f) == "prompt-injection" for f in findings)
    if any(f.severity == "CRITICAL" for f in findings) or manipulated or overall < settings.hitl_threshold:
        verdict = "NEEDS_HUMAN"
    elif any(f.severity == "HIGH" for f in findings):
        verdict = "REQUEST_CHANGES"
    else:
        verdict = "COMMENT"
    return round(overall, 3), verdict


def summary_markdown(review: Review, head_sha: str, blocked_actions: int, labels: list[str], gate: dict | None = None) -> str:
    findings = review.findings
    top = max((f.severity for f in findings), key=lambda s: _RANK[s], default="NONE")
    icon = {"CRITICAL": "🛑", "HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡", "INFO": "🔵"}
    lines = [
        f"## 🛡️ PRGuard review · `{head_sha[:7]}`",
        "",
        f"**Verdict:** `{review.verdict}` · **highest severity:** {top} · **findings:** {len(findings)}"
        f" · **confidence:** {review.overall_confidence:.2f}",
    ]
    if gate:
        lines.append(f"**CI gate:** {'❌ failed' if gate.get('failed') else '✅ passed'} — {gate.get('reason', '')}")
    if blocked_actions:
        lines.append(f"**Blocked actions:** {blocked_actions} (refused by PRGuard's access policy)")
    if review.verdict == "NEEDS_HUMAN":
        lines.append("\n> ⚠️ A human needs to look at this: a critical finding, low confidence, or text that tries to "
                     "instruct AI reviewers.")
    if not findings:
        lines.append("\nNo findings. ✅")
    for f in findings:
        where = f"`{f.file_path}:{f.line_start}`" if f.file_path else ""
        agreed = f" · agreed: {', '.join(f.agreed_by)}" if len(f.agreed_by) > 1 else ""
        lines.append(f"\n<details><summary>{icon.get(f.severity, '')} <b>{f.severity}</b> [{f.category}] {_html(f.summary)} {where}</summary>\n\n"
                     f"{f.rationale}\n\n**Suggestion:** {f.suggestion or '—'}\n\n"
                     f"<sub>{f.agent_type} · confidence {f.confidence:.2f} · checker: {f.checker_status}"
                     f"{(' — ' + _html(f.checker_reason)) if f.checker_reason else ''}{agreed}</sub>\n</details>")
    notes = [n for n in [review.checker_notes, *review.notes] if n]
    if notes:
        lines.append("\n<sub>" + " · ".join(_html(n) for n in notes) + "</sub>")
    if labels:
        lines.append(f"\n<sub>{' · '.join(labels)}</sub>")
    lines.append("\n<sub>Commands: `/review` · `/describe` · `/improve` · `/ask <question>`</sub>")
    return "\n".join(lines)


def _html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
