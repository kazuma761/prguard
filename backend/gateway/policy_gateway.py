"""PolicyGateway: evaluates prguard.policy.yaml for every call, then forwards
allowed calls to GitHub. Default deny; nothing reaches GitHub without a rule."""

from __future__ import annotations

from typing import Any

from backend.config import settings
from backend.gateway.base import BaseGateway, Upstream
from backend.gateway.policy_engine import PolicyEngine
from backend.models import Denial, Enforcer, TaskContext, ToolResult


class PolicyGateway(BaseGateway):
    enforcer = Enforcer.POLICY

    def __init__(self, upstream: Upstream, engine: PolicyEngine | None = None) -> None:
        self.upstream = upstream
        self.upstream_name = upstream.name
        self.engine = engine or PolicyEngine(settings.policy_path)

    async def _decide(self, agent_id: str, ctx: TaskContext, tool: str, args: dict[str, Any]) -> ToolResult | Denial:
        decision = self.engine.evaluate(agent_id, ctx, tool, args)
        if not decision.allowed:
            return Denial(tool=tool, reason=decision.reason, enforcer=self.enforcer, policy=decision.rule)
        return ToolResult(tool=tool, data=await self.upstream.call(tool, args))
