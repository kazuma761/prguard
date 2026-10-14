"""Pure policy evaluation for prguard.policy.yaml. No I/O beyond loading the file."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from backend.models import TaskContext


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    rule: str = ""


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Glob where `**/` spans zero or more directories and `*` stays in one."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += r"(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += r".*"
            i += 2
        elif pattern[i] == "*":
            out += r"[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(f"^{out}$")


def path_denied(path: str, patterns: list[str]) -> str | None:
    """Return the pattern that blocks `path`, if any."""
    clean = path.strip().lstrip("/")
    # Refuse traversal outright rather than trying to normalise it.
    if ".." in clean.split("/"):
        return ".."
    basename = clean.rsplit("/", 1)[-1]
    for pattern in patterns:
        rx = _glob_to_regex(pattern)
        if rx.match(clean) or ("/" not in pattern and rx.match(basename)):
            return pattern
    return None


class PolicyEngine:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.doc: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        always = self.doc.get("always_deny") or {}
        self.always_deny_tools: set[str] = set(always.get("tools") or [])
        self.cross_repo_denied: bool = bool(always.get("cross_repo", True))
        self.roles: dict[str, Any] = self.doc.get("roles") or {}

    def _task_value(self, template: Any, ctx: TaskContext) -> Any:
        mapping = {
            "{task.repo}": ctx.repo_full_name,
            "{task.pr_number}": ctx.pr_number,
            "{task.head_sha}": ctx.head_sha,
            "{task.base_ref}": ctx.base_ref,
        }
        return mapping.get(template, template) if isinstance(template, str) else template

    def evaluate(self, agent_id: str, ctx: TaskContext, tool: str, args: dict[str, Any]) -> PolicyDecision:
        repo = args.get("repo")
        if self.cross_repo_denied and repo is not None and repo != ctx.repo_full_name:
            return PolicyDecision(False, f"repo {repo} is outside this task ({ctx.repo_full_name})", "always_deny.cross_repo")

        if tool in self.always_deny_tools:
            return PolicyDecision(False, f"{tool} is never permitted for a PRGuard agent", "always_deny.tools")

        pull = args.get("pull_number")
        if pull is not None and str(pull) != str(ctx.pr_number):
            return PolicyDecision(False, f"pull_number {pull} is outside this task (PR #{ctx.pr_number})", "task_scope.pull_number")

        role = self.roles.get(agent_id)
        if role is None:
            return PolicyDecision(False, f"unknown agent {agent_id}", "default")

        near_miss = ""
        for index, rule in enumerate(role.get("allow") or []):
            tools = rule["tool"] if isinstance(rule["tool"], list) else [rule["tool"]]
            if tool not in tools:
                continue
            name = f"roles.{agent_id}.allow[{index}]"

            mismatch = ""
            for key, template in (rule.get("where") or {}).items():
                expected = self._task_value(template, ctx)
                actual = args.get(key)
                accepted = expected if isinstance(expected, list) else [expected]
                if actual is None or str(actual) not in {str(v) for v in accepted}:
                    mismatch = f"{key}={actual!r} not allowed (expected {' or '.join(str(v) for v in accepted)})"
                    break
            if mismatch:
                near_miss = near_miss or mismatch
                continue

            blocked = path_denied(str(args.get("path", "")), rule.get("deny_paths") or []) if "path" in args else None
            if blocked:
                return PolicyDecision(False, f"path {args['path']} matches denied pattern {blocked}", name)
            only = rule.get("allow_paths")
            matched = path_denied(str(args.get("path", "")), only) if only is not None else None
            if only is not None and matched in (None, ".."):  # ".." means traversal, never a match
                near_miss = near_miss or f"path {args.get('path')!r} is not one of {', '.join(only)}"
                continue

            if rule.get("requires") == "human_approved" and not ctx.human_approved:
                near_miss = "requires human approval for this task"
                continue

            return PolicyDecision(True, "allowed by role policy", name)

        if near_miss:
            return PolicyDecision(False, near_miss, f"roles.{agent_id}")
        return PolicyDecision(False, f"{tool} is not permitted for role {agent_id}", "default")
