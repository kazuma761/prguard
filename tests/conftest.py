"""Tests never touch the network: no real model, no real GitHub."""

import pytest

from backend.config import settings
from backend.events import bus
from backend.gateway import reset_gateways


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_model", "")
    monkeypatch.setattr(settings, "checker_model", "")
    monkeypatch.setattr(settings, "llm_cache", False)
    monkeypatch.setattr(settings, "upstream", "mock")
    monkeypatch.setattr(settings, "github_token", "")
    monkeypatch.setattr(settings, "target_repo", "demo/prguard-demo-target")
    monkeypatch.setattr(settings, "other_repo", "demo/prguard-demo-other")
    monkeypatch.setattr(settings, "runs_dir", tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    reset_gateways()
    bus.clear()
    yield
    reset_gateways()
