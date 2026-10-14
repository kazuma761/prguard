"""Per-repository settings from `.prguard.toml`.

Loaded from the PR's BASE branch through the policy gateway (role
prguard-config), never from the PR head and never from the CI checkout, so a
pull request cannot change how it is itself reviewed or gated.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from typing import Any

from backend.models import SEVERITY_RANK

COMMANDS = ("review", "describe", "improve", "ask")

DEFAULT_IGNORE = [
    "**/*.lock", "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml", "**/poetry.lock", "**/Cargo.lock",
    "**/go.sum", "**/*.min.js", "**/*.min.css", "**/*.map", "**/dist/**", "**/build/**", "**/vendor/**",
    "**/node_modules/**", "**/*.svg", "**/*.png", "**/*.jpg", "**/*.gif", "**/*.ico", "**/*.pdf",
    "**/*.pb.go", "**/*_pb2.py", "**/__snapshots__/**",
]


@dataclass
class ReviewCfg:
    specialists: list[str] = field(default_factory=lambda: ["security", "quality", "tests", "docs"])
    extra_instructions: str = ""
    max_findings: int = 6
    # Also submit a formal REQUEST_CHANGES review when the verdict says so
    # (the persistent comment is always posted).
    request_changes: bool = False
    ci_fixer: bool = True


@dataclass
class GateCfg:
    # Fail the CI job when a finding at or above this severity survives the checker.
    fail_on: str = "HIGH"
    # Ignore findings below this confidence when gating (small models are noisy).
    min_confidence: float = 0.6
    fail_on_needs_human: bool = False
    # If the LLM server is down or errors, fail the job (true) or pass with a warning (false).
    fail_on_error: bool = False


@dataclass
class DescribeCfg:
    publish: str = "body"   # body (between PRGuard markers, author text kept) | comment
    extra_instructions: str = ""


@dataclass
class ImproveCfg:
    max_suggestions: int = 5
    min_importance: int = 4
    extra_instructions: str = ""


@dataclass
class AutoCfg:
    on_open: list[str] = field(default_factory=lambda: ["describe", "review"])
    on_push: list[str] = field(default_factory=lambda: ["review"])


@dataclass
class CommandsCfg:
    # Who may trigger /review, /describe, /improve, /ask from a PR comment.
    allowed_associations: list[str] = field(default_factory=lambda: ["OWNER", "MEMBER", "COLLABORATOR"])


@dataclass
class ChecksCfg:
    # Runs inside the network-less, read-only sandbox container. Empty = skip.
    command: str = ("pylint $(find . -name '*.py' -not -path './.*' | head -200); echo __PYLINT_EXIT=$?; "
                    "python -m pytest -q -p no:cacheprovider; echo __PYTEST_EXIT=$?")
    image: str = ""


@dataclass
class RepoConfig:
    review: ReviewCfg = field(default_factory=ReviewCfg)
    gate: GateCfg = field(default_factory=GateCfg)
    describe: DescribeCfg = field(default_factory=DescribeCfg)
    improve: ImproveCfg = field(default_factory=ImproveCfg)
    auto: AutoCfg = field(default_factory=AutoCfg)
    commands: CommandsCfg = field(default_factory=CommandsCfg)
    checks: ChecksCfg = field(default_factory=ChecksCfg)
    ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    source: str = "defaults"
    warnings: list[str] = field(default_factory=list)


def _fill(target: Any, data: dict[str, Any], section: str, warnings: list[str]) -> None:
    known = {f.name: f for f in fields(target)}
    for key, value in data.items():
        if key not in known:
            warnings.append(f"unknown setting [{section}] {key}")
            continue
        current = getattr(target, key)
        if isinstance(current, bool) and not isinstance(value, bool):
            warnings.append(f"[{section}] {key} must be true or false")
        elif isinstance(current, list) and not isinstance(value, list):
            warnings.append(f"[{section}] {key} must be a list")
        elif isinstance(current, (int, float)) and not isinstance(current, bool) and not isinstance(value, (int, float)):
            warnings.append(f"[{section}] {key} must be a number")
        else:
            setattr(target, key, value)


def parse(text: str, source: str = ".prguard.toml") -> RepoConfig:
    cfg = RepoConfig(source=source)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        cfg.warnings.append(f"{source} is not valid TOML ({exc}); using defaults")
        cfg.source = "defaults"
        return cfg
    for section, value in data.items():
        if section == "ignore":
            paths = value.get("paths") if isinstance(value, dict) else value
            if isinstance(paths, list):
                extend = isinstance(value, dict) and value.get("extend_defaults", True)
                cfg.ignore = (list(DEFAULT_IGNORE) if extend else []) + [str(p) for p in paths]
            continue
        target = getattr(cfg, section, None)
        if target is None or not isinstance(value, dict) or section in ("source", "warnings"):
            cfg.warnings.append(f"unknown section [{section}]")
            continue
        _fill(target, value, section, cfg.warnings)
    _validate(cfg)
    return cfg


def _validate(cfg: RepoConfig) -> None:
    fail_on = str(cfg.gate.fail_on).upper()
    if fail_on not in SEVERITY_RANK and fail_on != "NONE":
        cfg.warnings.append(f"[gate] fail_on={cfg.gate.fail_on!r} is not a severity; using HIGH")
        fail_on = "HIGH"
    cfg.gate.fail_on = fail_on
    roles = ("security", "quality", "tests", "docs")
    bad = [s for s in cfg.review.specialists if s not in roles]
    if bad:
        cfg.warnings.append(f"[review] unknown specialists {bad}; known: {list(roles)}")
        cfg.review.specialists = [s for s in cfg.review.specialists if s in roles]
    for name in ("on_open", "on_push"):
        value = [c for c in getattr(cfg.auto, name) if c in COMMANDS and c != "ask"]
        setattr(cfg.auto, name, value)
    if cfg.describe.publish not in ("body", "comment"):
        cfg.warnings.append("[describe] publish must be body or comment; using body")
        cfg.describe.publish = "body"
    cfg.review.max_findings = max(1, min(int(cfg.review.max_findings), 15))
    cfg.improve.max_suggestions = max(0, min(int(cfg.improve.max_suggestions), 15))


def defaults() -> RepoConfig:
    return RepoConfig()
