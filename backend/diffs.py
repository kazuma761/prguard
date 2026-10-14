"""Turn a PR's patches into what a small model can actually use.

- Hunks are rendered with the NEW file's line numbers on every line, so a
  model can cite exact lines without seeing whole files.
- Lock files, build output, minified and generated files are dropped.
- The result is fitted to a token budget (an 8B model on a shared llama.cpp
  slot may only have a few thousand tokens); what did not fit is listed by name.
- `commentable_lines` tells /improve which lines GitHub accepts inline comments on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from backend.gateway.policy_engine import path_denied

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def est_tokens(text: str) -> int:
    """Rough token count for code (about 3 characters per token for Llama/Gemma tokenizers)."""
    return len(text) // 3 + 1


@dataclass
class Hunk:
    header: str
    lines: list[tuple[str, int | None, str]] = field(default_factory=list)  # (kind, new line no, text)


def parse_patch(patch: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    new_no = 0
    current: Hunk | None = None
    for raw in (patch or "").splitlines():
        match = _HUNK.match(raw)
        if match:
            new_no = int(match.group(3))
            current = Hunk(header=raw)
            hunks.append(current)
            continue
        if current is None or raw.startswith(("diff --git", "index ", "--- ", "+++ ")):
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        kind = raw[:1] if raw[:1] in "+- " else " "
        text = raw[1:] if raw[:1] in "+- " else raw
        if kind == "-":
            current.lines.append(("-", None, text))
        else:
            current.lines.append((kind, new_no, text))
            new_no += 1
    return hunks


def commentable_lines(patch: str) -> set[int]:
    """New-side line numbers GitHub accepts a review comment on (added or context lines)."""
    return {no for hunk in parse_patch(patch) for kind, no, _ in hunk.lines if no is not None}


def added_lines(patch: str) -> set[int]:
    return {no for hunk in parse_patch(patch) for kind, no, _ in hunk.lines if kind == "+" and no is not None}


def render_file(path: str, patch: str, status: str = "modified") -> str:
    out = [f"## File: {path} ({status})"]
    for hunk in parse_patch(patch):
        context = hunk.header.split("@@")[-1].strip()
        out.append(f"@@ {context}" if context else "@@")
        for kind, no, text in hunk.lines:
            number = f"{no:>5}" if no is not None else "     "
            out.append(f"{number} {kind} {text}")
    return "\n".join(out)


def split_unified_diff(diff: str) -> list[dict[str, Any]]:
    """Fallback when only a whole unified diff is available."""
    files: list[dict[str, Any]] = []
    for block in re.split(r"(?m)^diff --git ", diff or ""):
        if not block.strip():
            continue
        match = re.search(r"(?m)^\+\+\+ b/(.+)$", block) or re.search(r"^a/\S+ b/(\S+)", block)
        if not match:
            continue
        status = "added" if "\nnew file mode" in block else "removed" if "\ndeleted file mode" in block else "modified"
        files.append({"filename": match.group(1).strip(), "status": status, "patch": block[block.find("@@"):] if "@@" in block else ""})
    return files


@dataclass
class PromptDiff:
    text: str
    included: list[str]
    omitted: list[str]
    ignored: list[str]
    clipped: list[str]

    def note(self) -> str:
        parts = []
        if self.omitted:
            parts.append(f"{len(self.omitted)} file(s) left out to fit the model's context: {', '.join(self.omitted[:15])}")
        if self.clipped:
            parts.append(f"clipped: {', '.join(self.clipped[:10])}")
        if self.ignored:
            parts.append(f"{len(self.ignored)} generated/lock/binary file(s) skipped")
        return "; ".join(parts)


def build_prompt_diff(files: list[dict[str, Any]], ignore: list[str], budget_tokens: int) -> PromptDiff:
    """Render as many changed files as fit in `budget_tokens`, most-changed source first."""
    ignored = [f["filename"] for f in files if path_denied(f["filename"], ignore)]
    candidates = [f for f in files if f["filename"] not in ignored and f.get("status") != "removed" and f.get("patch")]
    removed = [f["filename"] for f in files if f.get("status") == "removed"]

    def priority(f: dict[str, Any]) -> tuple[int, int]:
        name = f["filename"].lower()
        is_test = "test" in name
        is_doc = name.endswith((".md", ".rst", ".txt"))
        changed = int(f.get("additions") or 0) + int(f.get("deletions") or 0) or f["patch"].count("\n")
        return (2 if is_doc else 1 if is_test else 0, -changed)

    budget = budget_tokens
    chunks, included, omitted, clipped = [], [], [], []
    for f in sorted(candidates, key=priority):
        rendered = render_file(f["filename"], f["patch"], f.get("status", "modified"))
        cost = est_tokens(rendered)
        if cost <= budget:
            chunks.append(rendered)
            included.append(f["filename"])
            budget -= cost
        elif budget > 400:
            keep = rendered[: budget * 3 - 200]
            keep = keep[: keep.rfind("\n")] if "\n" in keep else keep
            chunks.append(keep + "\n... (rest of this file's diff clipped to fit the context window)")
            included.append(f["filename"])
            clipped.append(f["filename"])
            budget = 0
        else:
            omitted.append(f["filename"])
    if removed:
        chunks.append("## Deleted files: " + ", ".join(removed[:30]))
    return PromptDiff(text="\n\n".join(chunks), included=included, omitted=omitted, ignored=ignored, clipped=clipped)
