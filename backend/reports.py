"""CI outputs: the pass/fail gate, JSON, SARIF 2.1.0 and a Markdown summary.

The gate is plain code over the checked findings, so it is deterministic and a
PR cannot talk its way past it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from backend.models import SEVERITY_RANK, CommandResult, Finding, Review
from backend.repo_config import GateCfg

SARIF_LEVEL = {"CRITICAL": "error", "HIGH": "error", "MEDIUM": "warning", "LOW": "note", "INFO": "note"}
SECURITY_SEVERITY = {"CRITICAL": "9.5", "HIGH": "8.0", "MEDIUM": "5.5", "LOW": "3.0", "INFO": "0.0"}


def gate(review: Review, cfg: GateCfg) -> dict[str, Any]:
    """Decide whether the pipeline should fail."""
    threshold = cfg.fail_on.upper()
    if threshold == "NONE":
        blocking: list[Finding] = []
    else:
        blocking = [f for f in review.findings
                    if SEVERITY_RANK[f.severity] >= SEVERITY_RANK[threshold]
                    and f.confidence >= cfg.min_confidence
                    and f.checker_status != "REJECTED"]
    failed = bool(blocking) or (cfg.fail_on_needs_human and review.verdict == "NEEDS_HUMAN")
    if blocking:
        reason = f"{len(blocking)} finding(s) at {threshold} or above with confidence ≥ {cfg.min_confidence}"
    elif failed:
        reason = "review needs a human (fail_on_needs_human = true)"
    elif threshold == "NONE":
        reason = "gate disabled (fail_on = none)"
    else:
        reason = f"no finding at {threshold} or above with confidence ≥ {cfg.min_confidence}"
    return {"failed": failed, "reason": reason, "fail_on": threshold, "min_confidence": cfg.min_confidence,
            "blocking": [f"{f.severity} {f.file_path}:{f.line_start} {f.summary}" for f in blocking]}


def to_sarif(result: CommandResult) -> dict[str, Any]:
    findings = result.review.findings if result.review else []
    rules: dict[str, dict[str, Any]] = {}
    sarif_results = []
    for f in findings:
        rule_id = f"prguard/{f.agent_type}/{f.category}"
        rules.setdefault(rule_id, {
            "id": rule_id, "name": f.category,
            "shortDescription": {"text": f.category.replace("-", " ")},
            "properties": {"tags": [f.agent_type, "prguard"],
                           **({"security-severity": SECURITY_SEVERITY[f.severity]} if f.agent_type == "security" else {})},
        })
        entry: dict[str, Any] = {
            "ruleId": rule_id,
            "level": SARIF_LEVEL[f.severity],
            "message": {"text": f"{f.summary}\n\n{f.rationale}\n\nSuggestion: {f.suggestion}".strip()},
            "properties": {"severity": f.severity, "confidence": f.confidence, "checker": f.checker_status},
        }
        if f.file_path:
            region = {"startLine": max(1, f.line_start)}
            if f.line_end and f.line_end >= f.line_start:
                region["endLine"] = max(1, f.line_end)
            entry["locations"] = [{"physicalLocation": {"artifactLocation": {"uri": f.file_path}, "region": region}}]
        sarif_results.append(entry)
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "PRGuard", "informationUri": "https://github.com/kazuma761/prguard",
                                "rules": list(rules.values())}},
            "results": sarif_results,
            "properties": {"repo": result.repo, "pull_request": result.pr_number, "commit": result.head_sha},
        }],
    }


def to_markdown(result: CommandResult) -> str:
    if result.review and result.review.summary_markdown:
        return result.review.summary_markdown
    lines = [f"## PRGuard `{result.command}` · {result.repo}#{result.pr_number}"]
    if not result.ok:
        lines.append(f"\n⚠️ {result.error}")
    if result.description:
        lines.append(f"\n{result.description.summary}")
    if result.suggestions:
        lines.append(f"\n{len(result.suggestions)} suggestion(s) posted inline.")
    if result.answer:
        lines.append(f"\n{result.answer}")
    return "\n".join(lines)


def write(results: list[CommandResult], out_dir: Path) -> dict[str, str]:
    """Write prguard-report.json, prguard.sarif and prguard-summary.md. Returns their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": out_dir / "prguard-report.json",
        "sarif": out_dir / "prguard.sarif",
        "markdown": out_dir / "prguard-summary.md",
    }
    paths["json"].write_text(json.dumps([r.model_dump(mode="json") for r in results], indent=2), encoding="utf-8")
    review = next((r for r in results if r.review is not None), None)
    sarif = to_sarif(review) if review else to_sarif(results[0]) if results else {"version": "2.1.0", "runs": []}
    paths["sarif"].write_text(json.dumps(sarif, indent=2), encoding="utf-8")
    markdown = "\n\n---\n\n".join(to_markdown(r) for r in results)
    paths["markdown"].write_text(markdown, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
    return {k: str(v) for k, v in paths.items()}
