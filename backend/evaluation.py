"""`prguard eval`: score the configured models on labelled pull requests.

Runs a full review (offline GitHub fixtures from demo_target/, nothing is
posted) for every PR whose PR.json has an "expected" list of topics, and
reports recall (expected problems found), noise (findings on other topics)
and time. Use it to compare models or prompts before rolling a change out:

    LLM_MODEL=llama-3.1-8b  CHECKER_MODEL=gemma-4-12b prguard eval
    LLM_MODEL=gemma-4-12b   CHECKER_MODEL=llama-3.1-8b prguard eval

Add your own cases by adding folders under demo_target/prs/.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from backend.agents import aggregator
from backend.config import settings


async def evaluate(out_dir: Path, runs: int = 1) -> int:
    from backend import commands, triggers
    from backend.context import Options
    from backend.gateway import mock_upstream, reset_gateways

    settings.upstream = "mock"
    reset_gateways()
    upstream = mock_upstream()
    cases = {n: meta for n, meta in sorted(upstream.prs.items()) if meta.get("expected")}
    if not cases:
        print("no labelled PRs found (add \"expected\": [topics] to demo_target/prs/*/PR.json)")
        return 2
    model = "MOCK LLM" if settings.llm_is_mock else settings.llm_model
    print(f"PRGuard eval · reviewers {model} · checker {settings.checker[2] if settings.checker else 'none'} · {runs} run(s) per PR")
    rows = []
    for number, meta in cases.items():
        expected = set(meta["expected"])
        for run in range(runs):
            upstream.reset()
            ctx = await triggers.context_for_pr(upstream_gateway(), settings.target_repo, number)
            started = time.monotonic()
            result = await commands.run(ctx, "review", Options(publish=False))
            seconds = time.monotonic() - started
            findings = result.review.findings if result.review else []
            topics = {aggregator.topic(f) for f in findings}
            found = expected & topics
            noise = [f for f in findings if aggregator.topic(f) not in expected]
            rows.append({"pr": number, "title": meta["title"], "run": run + 1, "ok": result.ok, "error": result.error,
                         "expected": sorted(expected), "found": sorted(found), "missed": sorted(expected - topics),
                         "recall": round(len(found) / len(expected), 2), "findings": len(findings), "noise": len(noise),
                         "verdict": result.review.verdict if result.review else "", "seconds": round(seconds, 1),
                         "gate_failed": bool(result.gate.get("failed"))})
            r = rows[-1]
            print(f"  PR #{number} run {run + 1}: recall {r['recall']:.2f} ({len(found)}/{len(expected)})  noise {r['noise']}  "
                  f"verdict {r['verdict'] or '-'}  {r['seconds']}s" + (f"  missed {r['missed']}" if r["missed"] else "")
                  + (f"  ERROR {r['error'][:80]}" if not r["ok"] else ""))
    recall = sum(r["recall"] for r in rows) / len(rows)
    noise = sum(r["noise"] for r in rows) / len(rows)
    seconds = sum(r["seconds"] for r in rows) / len(rows)
    print(f"overall: recall {recall:.2f} · noise {noise:.1f} findings/PR · {seconds:.1f}s/PR")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "prguard-eval.json"
    path.write_text(json.dumps({"reviewer": model, "checker": settings.checker[2] if settings.checker else None,
                                "recall": round(recall, 3), "noise": round(noise, 2), "seconds": round(seconds, 1),
                                "rows": rows}, indent=2), encoding="utf-8")
    print(f"written: {path}")
    return 0


def upstream_gateway():
    from backend.gateway import gateway

    return gateway()
