"""Settings, read once from the environment (.env is loaded if present).

Everything runs locally: the reviewers and the checker talk to any
OpenAI-compatible server (llama.cpp, Ollama, vLLM, LM Studio), and GitHub is
reached directly over its REST API (github.com or GitHub Enterprise Server).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() not in ("0", "false", "no", "off")


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


@dataclass
class Settings:
    # ---- models (any OpenAI-compatible endpoint) -------------------------
    # LLM_PROVIDER: "local" (default when LLM_BASE_URL is set) or "mock".
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER").lower())
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL").rstrip("/"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL"))
    # Independent checker: ideally a different model family from the reviewers.
    # CHECKER_BASE_URL defaults to LLM_BASE_URL (one server can host both models).
    checker_base_url: str = field(default_factory=lambda: _env("CHECKER_BASE_URL").rstrip("/"))
    checker_api_key: str = field(default_factory=lambda: _env("CHECKER_API_KEY"))
    checker_model: str = field(default_factory=lambda: _env("CHECKER_MODEL"))
    # Context window of the served model (llama.cpp: per-slot ctx = --ctx-size / --parallel).
    llm_context_tokens: int = field(default_factory=lambda: _int("LLM_CONTEXT_TOKENS", 8192))
    llm_max_output_tokens: int = field(default_factory=lambda: _int("LLM_MAX_OUTPUT_TOKENS", 1500))
    # How many requests PRGuard sends at once. Keep it low on a shared GPU.
    llm_concurrency: int = field(default_factory=lambda: _int("LLM_CONCURRENCY", 2))
    llm_timeout_s: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_S") or 180))
    # Ask the server for a JSON object (llama.cpp, Ollama and vLLM support it).
    llm_json_mode: bool = field(default_factory=lambda: _flag("LLM_JSON_MODE", True))
    # Let specialists call read-only tools. Off by default: small models are
    # unreliable at tool use, and llama.cpp needs --jinja for it.
    llm_tools: bool = field(default_factory=lambda: _flag("LLM_TOOLS", False))
    # Replay identical LLM requests from disk (deterministic re-runs, no repeat cost).
    llm_cache: bool = field(default_factory=lambda: _flag("LLM_CACHE", False))

    # ---- GitHub ----------------------------------------------------------
    # github = real GitHub REST API, mock = offline fixtures from demo_target/.
    upstream: str = field(default_factory=lambda: _env("UPSTREAM", "github").lower())
    github_token: str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    # GitHub Actions sets both; for GitHub Enterprise Server use https://ghe.example.com/api/v3.
    github_api_url: str = field(default_factory=lambda: _env("GITHUB_API_URL", "https://api.github.com").rstrip("/"))
    github_server_url: str = field(default_factory=lambda: _env("GITHUB_SERVER_URL", "https://github.com").rstrip("/"))
    # Login PRGuard posts as. Used to find its own persistent comments; GitHub
    # App / Actions tokens cannot call /user, so this is the fallback.
    github_bot_login: str = field(default_factory=lambda: _env("GITHUB_BOT_LOGIN", "github-actions[bot]"))
    github_webhook_secret: str = field(default_factory=lambda: _env("GITHUB_WEBHOOK_SECRET"))
    # Server mode: comma-separated repos the webhook may act on ("*" = any repo the token can reach).
    allowed_repos: str = field(default_factory=lambda: _env("PRGUARD_ALLOWED_REPOS", "*"))
    # Offline demo / tests only.
    target_repo: str = field(default_factory=lambda: _env("TARGET_REPO", "demo/prguard-demo-target"))
    other_repo: str = field(default_factory=lambda: _env("OTHER_REPO", "demo/prguard-demo-other"))

    # ---- sandbox (runs the PR's linters/tests with no network) -------------
    sandbox_image: str = field(default_factory=lambda: _env("SANDBOX_IMAGE", "prguard-sandbox"))
    sandbox_timeout_s: int = field(default_factory=lambda: _int("SANDBOX_TIMEOUT_S", 120))
    # When PRGuard itself runs in a container and starts the sandbox as a sibling
    # container through the Docker socket, bind mounts are resolved on the HOST.
    # Set this to the host path of RUNS_DIR so checkouts can be mounted.
    sandbox_host_runs_dir: str = field(default_factory=lambda: _env("SANDBOX_HOST_RUNS_DIR"))

    hitl_threshold: float = field(default_factory=lambda: float(_env("HITL_THRESHOLD") or 0.7))
    node_timeout_s: float = field(default_factory=lambda: float(_env("NODE_TIMEOUT_S") or 300))

    runs_dir: Path = field(default_factory=lambda: Path(_env("RUNS_DIR") or ROOT / "runs"))
    policy_path: Path = field(default_factory=lambda: Path(_env("POLICY_PATH") or ROOT / "backend" / "policies" / "prguard.policy.yaml"))
    demo_target_dir: Path = ROOT / "demo_target"
    ui_dist_dir: Path = field(default_factory=lambda: Path(_env("UI_DIST_DIR") or ROOT / "frontend" / "dist"))

    @property
    def provider(self) -> str:
        """Which backend the reviewers use: local | mock."""
        if self.llm_provider == "mock":
            return "mock"
        return "local" if self.llm_base_url and self.llm_model else "mock"

    @property
    def llm_is_mock(self) -> bool:
        return self.provider == "mock"

    @property
    def checker(self) -> tuple[str, str, str] | None:
        """(base_url, api_key, model) for the independent checker, or None."""
        if self.llm_is_mock or not self.checker_model or self.checker_model.lower() == "none":
            return None
        return (self.checker_base_url or self.llm_base_url, self.checker_api_key or self.llm_api_key, self.checker_model)

    @property
    def checker_available(self) -> bool:
        return self.checker is not None

    def repo_allowed(self, repo: str) -> bool:
        names = {n.strip().lower() for n in self.allowed_repos.split(",") if n.strip()}
        return "*" in names or repo.lower() in names


settings = Settings()
