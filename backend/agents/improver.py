"""/improve: concrete code suggestions as inline GitHub suggestion blocks.

Every suggestion the model writes is checked by code before it is posted:
the file must be in the PR, every line must be one GitHub accepts a comment
on, at least one line must be added by this PR, and `existing_code` must match
the file at the head commit (if it does not, the suggestion is moved to where
that code really is, or dropped). Authors apply them with one click.
"""

from __future__ import annotations

import re
from typing import Any

from backend import context as ctxmod
from backend import diffs
from backend.agents import llm, prompts
from backend.agents.specialists import extract_items
from backend.config import settings
from backend.gateway.base import ToolGateway
from backend.models import CodeSuggestion, Denial, TaskContext, UpstreamError

AGENT_ID = "prguard-improver"
MAX_SPAN = 30
_LABELS = {"bug", "security", "performance", "error-handling", "maintainability", "best-practice", "style"}


def _norm(text: str) -> str:
    return "\n".join(line.strip() for line in (text or "").strip("\n").splitlines()).strip()


def _strip_numbers(code: str) -> str:
    """Models sometimes copy the line numbers and +/- markers from the prompt."""
    lines = (code or "").split("\n")
    if lines and all(re.match(r"^\s*\d+ [+ -] ", line) or not line.strip() for line in lines):
        return "\n".join(re.sub(r"^\s*\d+ [+ -] ", "", line) for line in lines)
    return code or ""


def _relocate(file_lines: list[str], existing: str, allowed: set[int]) -> tuple[int, int] | None:
    want = _norm(existing).split("\n")
    if not want or not want[0]:
        return None
    span = len(want)
    for start in range(1, len(file_lines) - span + 2):
        window = [line.strip() for line in file_lines[start - 1:start - 1 + span]]
        if window == want and all(n in allowed for n in range(start, start + span)):
            return start, start + span - 1
    return None


def validate(raw: list[dict[str, Any]], files: list[dict[str, Any]], head_files: dict[str, str],
             min_importance: int, limit: int) -> tuple[list[CodeSuggestion], list[str]]:
    patches = {f["filename"]: f.get("patch", "") for f in files}
    kept: list[CodeSuggestion] = []
    dropped: list[str] = []
    for item in raw:
        path = str(item.get("file_path") or item.get("file") or "").strip().lstrip("/")
        try:
            start, end = int(item.get("line_start")), int(item.get("line_end") or item.get("line_start"))
            importance = max(1, min(10, int(float(item.get("importance", 5)))))
        except (TypeError, ValueError):
            dropped.append(f"{path}: bad line numbers")
            continue
        improved = _strip_numbers(str(item.get("improved_code") or "")).rstrip("\n")
        existing = _strip_numbers(str(item.get("existing_code") or "")).rstrip("\n")
        summary = str(item.get("summary") or "").strip()[:200]
        if path not in patches or not summary or not improved.strip():
            dropped.append(f"{path}: not a changed file or empty suggestion")
            continue
        if importance < min_importance:
            dropped.append(f"{path}:{start}: importance {importance} below {min_importance}")
            continue
        allowed = diffs.commentable_lines(patches[path])
        added = diffs.added_lines(patches[path])
        file_lines = head_files.get(path, "").split("\n") if path in head_files else []
        if end < start or end - start + 1 > MAX_SPAN:
            dropped.append(f"{path}:{start}-{end}: bad range")
            continue
        if file_lines and existing.strip():
            actual = "\n".join(file_lines[start - 1:end])
            if _norm(actual) != _norm(existing):
                moved = _relocate(file_lines, existing, allowed)
                if not moved:
                    dropped.append(f"{path}:{start}: existing_code does not match the file")
                    continue
                start, end = moved
        if not all(n in allowed for n in range(start, end + 1)) or not any(n in added for n in range(start, end + 1)):
            dropped.append(f"{path}:{start}-{end}: lines are not part of this PR's changes")
            continue
        if file_lines and _norm("\n".join(file_lines[start - 1:end])) == _norm(improved):
            dropped.append(f"{path}:{start}: suggestion changes nothing")
            continue
        label = str(item.get("label") or "maintainability").lower().strip()
        kept.append(CodeSuggestion(file_path=path, line_start=start, line_end=end,
                                   existing_code="\n".join(file_lines[start - 1:end]) if file_lines else existing,
                                   improved_code=improved, summary=summary,
                                   label=label if label in _LABELS else "maintainability", importance=importance))
    kept.sort(key=lambda s: -s.importance)
    # One suggestion per line range.
    unique: list[CodeSuggestion] = []
    for s in kept:
        if not any(u.file_path == s.file_path and u.line_start <= s.line_end and s.line_start <= u.line_end for u in unique):
            unique.append(s)
    return unique[:limit], dropped


def mock(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic stand-in: tighten an amount check that lets negative values through."""
    out = []
    for f in context.get("files", []):
        for hunk in diffs.parse_patch(f.get("patch", "")):
            for kind, no, text in hunk.lines:
                if kind == "+" and re.search(r"if (\w+) > balance:", text):
                    var = re.search(r"if (\w+) > balance:", text).group(1)
                    out.append({"file_path": f["filename"], "line_start": no, "line_end": no, "existing_code": text,
                                "improved_code": text.replace(f"if {var} > balance:", f"if {var} <= 0 or {var} > balance:"),
                                "summary": "[MOCK LLM] Reject non-positive amounts, not only amounts above the balance",
                                "label": "bug", "importance": 8})
    return out


def comment_body(s: CodeSuggestion) -> str:
    return (f"**{s.label}** · importance {s.importance}/10 — {s.summary}\n\n```suggestion\n{s.improved_code}\n```\n"
            f"<sub>PRGuard /improve</sub>")


def summary_markdown(suggestions: list[CodeSuggestion], head_sha: str, dropped: int) -> str:
    lines = [f"### 🛡️ PRGuard code suggestions · `{head_sha[:7]}`", ""]
    if not suggestions:
        lines.append("No suggestions worth making. ✅")
    else:
        lines += ["| Importance | Label | Where | Suggestion |", "|---|---|---|---|"]
        lines += [f"| {s.importance}/10 | {s.label} | `{s.file_path}:{s.line_start}` | {s.summary.replace('|', '/')} |"
                  for s in suggestions]
        lines += ["", "Each one is posted inline on the diff; use **Commit suggestion** to apply it."]
    if dropped:
        lines.append(f"\n<sub>{dropped} model suggestion(s) were discarded by validation (wrong lines, code not in this PR, or low importance).</sub>")
    return "\n".join(lines)


async def run(ctx: TaskContext, context: dict[str, Any], options: ctxmod.Options,
              gateway: ToolGateway) -> tuple[list[CodeSuggestion], list[str], dict[str, Any]]:
    cfg = context["config"]
    usage: dict[str, Any] = {}
    if settings.llm_is_mock:
        raw = mock(context)
    else:
        result = await llm.chat(prompts.improve_system(cfg.improve.max_suggestions, cfg.improve.extra_instructions),
                                ctxmod.pr_prompt(context), max_tokens=max(settings.llm_max_output_tokens, 2000))
        usage = result.usage()
        raw = extract_items(result.text, "suggestions")
    suggestions, dropped = validate(raw, context["files"], context["head_files"], cfg.improve.min_importance,
                                    cfg.improve.max_suggestions)
    usage["dropped"] = dropped

    posted: list[str] = []
    if options.publish:
        try:
            if suggestions:
                comments = []
                for s in suggestions:
                    comment: dict[str, Any] = {"path": s.file_path, "line": s.line_end, "side": "RIGHT", "body": comment_body(s)}
                    if s.line_end > s.line_start:
                        comment.update(start_line=s.line_start, start_side="RIGHT")
                    comments.append(comment)
                res = await gateway.call(AGENT_ID, ctx, "create_pull_request_review",
                                         {"repo": ctx.repo_full_name, "pull_number": ctx.pr_number, "event": "COMMENT",
                                          "commit_id": ctx.head_sha, "comments": comments,
                                          "body": f"PRGuard: {len(suggestions)} code suggestion(s) for `{ctx.head_sha[:7]}`."})
                if not isinstance(res, Denial) and isinstance(res.data, dict) and res.data.get("html_url"):
                    posted.append(res.data["html_url"])
            res = await gateway.call(AGENT_ID, ctx, "upsert_issue_comment",
                                     {"repo": ctx.repo_full_name, "pull_number": ctx.pr_number, "marker": "improve",
                                      "body": summary_markdown(suggestions, ctx.head_sha, len(dropped))})
            if not isinstance(res, Denial) and isinstance(res.data, dict) and res.data.get("html_url"):
                posted.append(res.data["html_url"])
        except UpstreamError as exc:
            usage["publish_error"] = str(exc)[:300]
    return suggestions, posted, usage
