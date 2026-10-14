"""Independent checker ("maker != checker").

A second model (ideally another family: Gemma checking Llama, say) re-checks
each finding against the diff. It sees the findings and the code only, never
the reviewers' reasoning, and can confirm, downgrade or reject but never add.
"""

from __future__ import annotations

import json
from typing import Any

from backend.agents import llm, prompts
from backend.agents.specialists import extract_items
from backend.config import settings
from backend.models import SEVERITY_RANK, Finding

AGENT_ID = "prguard-checker"


async def check(findings: list[Finding], diff_text: str) -> tuple[list[Finding], str, dict[str, Any]]:
    """Return (findings with REJECTED ones dropped, notes, llm usage)."""
    if not findings:
        return findings, "", {}
    endpoint = llm.checker_endpoint()
    if endpoint is None:
        return findings, "independent checker not configured (CHECKER_MODEL); findings are unverified", {}

    payload = [
        {"index": i, **f.model_dump(include={"severity", "category", "summary", "file_path", "line_start", "line_end", "rationale"})}
        for i, f in enumerate(findings)
    ]
    user = f"FINDINGS:\n{json.dumps(payload, indent=1)}\n\n<pr_diff>\n{prompts.defang(diff_text)}\n</pr_diff>"
    try:
        result = await llm.chat(prompts.CHECKER, user, endpoint=endpoint, max_tokens=1200)
    except llm.LLMError as exc:
        return findings, f"independent checker failed ({str(exc)[:120]}); findings are unverified", {}
    verdicts = extract_items(result.text, "verdicts")
    if not verdicts:
        return findings, f"checker ({endpoint.model}) returned no usable verdicts; findings are unverified", result.usage()

    by_index = {v.get("index"): v for v in verdicts}
    kept: list[Finding] = []
    rejected = 0
    for i, finding in enumerate(findings):
        verdict = by_index.get(i)
        if not verdict:
            kept.append(finding)
            continue
        status = str(verdict.get("status", "")).upper()
        reason = str(verdict.get("reason", ""))[:300]
        if status == "REJECTED":
            rejected += 1
            continue
        update: dict[str, Any] = {
            "checker_status": status if status in ("CONFIRMED", "DOWNGRADED") else "UNVERIFIED",
            "checker_reason": reason,
        }
        new_severity = str(verdict.get("severity", "")).upper()
        # A checker may lower a severity, never raise it.
        if status == "DOWNGRADED" and new_severity in SEVERITY_RANK and SEVERITY_RANK[new_severity] < SEVERITY_RANK[finding.severity]:
            update["severity"] = new_severity
        kept.append(finding.model_copy(update=update))

    notes = f"{endpoint.model} checked {len(findings)} findings, rejected {rejected}"
    if llm.family(endpoint.model) == llm.family(settings.llm_model):
        notes += " (warning: same model family as the reviewers, so this is a weaker check)"
    return kept, notes, result.usage()
