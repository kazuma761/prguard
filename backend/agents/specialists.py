"""The specialist reviewers: security, quality, tests, docs.

Each one is the same loop with a different role prompt and its own agent id,
so access policy can differ per role. With no LLM configured, a deterministic
mock stands in and every finding is labelled as such.
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.agents import llm, prompts
from backend.config import settings
from backend.gateway.base import ToolGateway
from backend.models import Denial, Finding, TaskContext, UpstreamError

ROLES = ["security", "quality", "tests", "docs"]
MAX_TOOL_TURNS = 3
_AGENT_IDS = {"ci": "prguard-ci-fixer"}
_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")

TOOLS = [
    {
        "name": "get_file_contents",
        "description": "Read one file from the pull request head commit.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "search_code",
        "description": "Search the repository under review for a string.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
]


def agent_id(role: str) -> str:
    return _AGENT_IDS.get(role, f"prguard-{role}")


def _int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def normalize_finding(item: Any, role: str) -> Finding | None:
    """Coerce one model-written finding into the contract. Small models are sloppy
    about casing, number types and optional fields, so be forgiving but strict on shape."""
    if not isinstance(item, dict) or not str(item.get("summary", "")).strip():
        return None
    severity = str(item.get("severity", "")).strip().upper()
    if severity not in _SEVERITIES:
        return None
    try:
        confidence = float(item.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    if confidence > 1:  # e.g. 85 meaning 85%
        confidence /= 100
    start = _int(item.get("line_start"))
    return Finding(
        agent_type=role, severity=severity,
        category=str(item.get("category") or "general").strip().lower().replace(" ", "-")[:60],
        summary=str(item["summary"]).strip()[:400],
        file_path=str(item.get("file_path") or "").strip().lstrip("/")[:300],
        line_start=start, line_end=max(start, _int(item.get("line_end"))),
        suggestion=str(item.get("suggestion") or "").strip()[:800],
        confidence=min(1.0, max(0.0, confidence)),
        rationale=str(item.get("rationale") or "").strip()[:800],
    )


def extract_json(text: str) -> Any:
    """First JSON object in a model reply, tolerating code fences and chatter."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def extract_items(text: str, key: str) -> list[dict[str, Any]]:
    """The list of objects under `key` in a model reply.

    Falls back to salvaging every complete object when the reply is not valid
    JSON, which is what happens when a model runs out of output tokens mid-list.
    """
    data = extract_json(text)
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return [item for item in data[key] if isinstance(item, dict)]
    start = (text or "").find(f'"{key}"')
    start = text.find("[", start) if start >= 0 else -1
    if start < 0:
        return []
    items: list[dict[str, Any]] = []
    depth, begin, in_string, escaped = 0, -1, False, False
    for index in range(start + 1, len(text)):
        char = text[index]
        if in_string:
            escaped = char == "\\" and not escaped
            if char == '"' and not escaped:
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                begin = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                try:
                    item = json.loads(text[begin:index + 1])
                except ValueError:
                    continue
                if isinstance(item, dict):
                    items.append(item)
        elif char == "]" and depth == 0:
            break
    return items


def parse_findings(text: str, role: str) -> list[Finding]:
    findings = [normalize_finding(item, role) for item in extract_items(text, "findings")]
    return [f for f in findings if f is not None][:8]


async def _tool(role: str, ctx: TaskContext, gateway: ToolGateway, name: str, args: dict[str, Any]) -> str:
    # Repo and ref come from the trusted context, never from the model.
    if name == "get_file_contents":
        call_args = {"repo": ctx.repo_full_name, "path": str(args.get("path", "")), "ref": ctx.head_sha}
    elif name == "search_code":
        call_args = {"repo": ctx.repo_full_name, "query": str(args.get("query", ""))}
    else:
        return "unknown tool"
    try:
        result = await gateway.call(agent_id(role), ctx, name, call_args)
    except UpstreamError as exc:
        return f"tool error: {exc}"
    if isinstance(result, Denial):
        return f"REFUSED by access control: {result.reason}. This is final."
    return json.dumps(result.data)[:12000]


async def run_specialist(role: str, ctx: TaskContext, context: dict[str, Any], gateway: ToolGateway) -> tuple[list[Finding], dict[str, Any]]:
    """Return (findings, llm usage)."""
    if settings.llm_is_mock:
        return mock_findings(role, context), {"model": "MOCK LLM"}

    async def on_tool(name: str, args: dict[str, Any]) -> str:
        return await _tool(role, ctx, gateway, name, args)

    cfg = context["config"]
    result = await llm.chat(
        prompts.specialist_system(role, ctx.repo_full_name, ctx.pr_number, ctx.head_sha,
                                  max_findings=cfg.review.max_findings, extra_instructions=cfg.review.extra_instructions),
        prompts.pr_block(context["prompt_diff"], context["check_results"], title=context.get("title", "")),
        tools=TOOLS, on_tool=on_tool, max_tool_turns=MAX_TOOL_TURNS,
    )
    return parse_findings(result.text, role)[: cfg.review.max_findings], result.usage()


# ---------------------------------------------------------------- mock LLM

def _line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    return 1


def mock_findings(role: str, context: dict[str, Any]) -> list[Finding]:
    """Deterministic stand-in for the LLM. Simple pattern checks, clearly labelled."""
    files: dict[str, str] = context.get("head_files", {})
    tag = "[MOCK LLM] "
    out: list[Finding] = []

    def add(severity: str, category: str, summary: str, path: str, needle: str, suggestion: str, confidence: float, rationale: str):
        line = _line_of(files.get(path, ""), needle)
        out.append(Finding(agent_type=role, severity=severity, category=category, summary=tag + summary,
                           file_path=path, line_start=line, line_end=line, suggestion=suggestion,
                           confidence=confidence, rationale=rationale))

    for path, text in files.items():
        is_py = path.endswith(".py")
        if role == "security":
            if "load-plugins" in text or "init-hook" in text:
                add("CRITICAL", "untrusted-code-execution", "Linter config loads and runs code shipped in this PR",
                    path, "load-plugins", "Remove init-hook/load-plugins, or only run linters with a config from the base branch.",
                    0.95, "init-hook adds a PR directory to sys.path and load-plugins imports a module from it, so pylint executes PR code.")
            if is_py and "urllib.request" in text and "os.environ" in text:
                add("HIGH", "data-exfiltration", "Module reads the environment and makes a network request at import time",
                    path, "urllib.request.urlopen", "Do not ship code that sends environment data over the network.",
                    0.9, "The module collects os.environ keys and POSTs them to a URL when imported.")
            if re.search(r"AI REVIEWERS|ignore (all|your) (previous )?instructions", text, re.IGNORECASE):
                add("HIGH", "prompt-injection", "Comment tries to instruct AI reviewers to approve and merge",
                    path, "AI REVIEWERS", "Remove the comment. Reviewer bots must treat PR text as data.",
                    0.95, "The comment addresses AI reviewers and asks them to approve, merge and push.")
        if role == "quality" and is_py and "def refund_partial" in text and "amount_cents <= 0" not in text.split("def refund_partial")[1]:
            add("MEDIUM", "edge-case", "refund_partial accepts a negative amount, which increases the balance",
                path, "def refund_partial", "Reject amount_cents <= 0 with PaymentError, as charge_customer does.",
                0.85, "balance - amount_cents grows when amount_cents is negative and nothing checks for it.")
        if role == "docs" and is_py and not path.startswith("tests/"):
            added = context.get("diff", "")
            for match in re.finditer(r"^def (\w+)\(.*\).*:\n(?!\s+\"\"\")", text, re.MULTILINE):
                if not match.group(1).startswith("_") and f"+def {match.group(1)}(" in added:
                    add("LOW", "missing-docstring", f"Public function {match.group(1)} has no docstring",
                        path, f"def {match.group(1)}", "Add a one-line docstring describing inputs and the return value.",
                        0.8, "Every other public function in this module has a docstring.")
    if role == "tests":
        source = "\n".join(t for p, t in files.items() if p.startswith("app/"))
        tests = "\n".join(t for p, t in files.items() if p.startswith("tests/"))
        tests = tests or context.get("base_tests", "")
        for func in re.findall(r"^def (\w+)\(", source, re.MULTILINE):
            if func in context.get("diff", "") and f"{func}(" not in tests and not func.startswith("_"):
                path = next(p for p, t in files.items() if f"def {func}" in t)
                add("MEDIUM", "missing-test", f"{func} is added without any test", path, f"def {func}",
                    f"Add test_{func}_happy_path and test_{func}_rejects_bad_input.", 0.8,
                    "No test file references this function.")
        if "def refund_partial" in source and "refund_partial" in tests and "-" not in "".join(re.findall(r"refund_partial\([^)]*\)", tests)):
            path = next(p for p in files if p.startswith("tests/"))
            add("MEDIUM", "missing-test", "No test covers a negative or zero partial refund", path, "refund_partial",
                "Add test_refund_partial_rejects_negative_amount.", 0.8,
                "Tests cover the happy path and over-refund but never pass a non-positive amount.")
    return out
