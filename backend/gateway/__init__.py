"""Gateway factory: one place decides which upstream is in play."""

from __future__ import annotations

from functools import lru_cache

from backend.config import settings
from backend.gateway.base import ToolGateway, Upstream
from backend.gateway.mock_upstream import MockUpstream
from backend.gateway.policy_gateway import PolicyGateway


@lru_cache
def mock_upstream() -> MockUpstream:
    return MockUpstream()


@lru_cache
def _upstream() -> Upstream:
    if settings.upstream == "mock":
        return mock_upstream()
    from backend.gateway.github import GitHubUpstream

    return GitHubUpstream()


@lru_cache
def _gateway() -> ToolGateway:
    return PolicyGateway(_upstream())


def gateway() -> ToolGateway:
    return _gateway()


def reset_gateways() -> None:
    """Forget cached gateways (tests, or after settings change)."""
    for cached in (mock_upstream, _upstream, _gateway):
        cached.cache_clear()
