"""/ask: answer a free-text question about the PR in a new comment."""

from __future__ import annotations

from typing import Any

from backend import context as ctxmod
from backend.agents import llm, prompts
from backend.agents.specialists import extract_json
from backend.config import settings
from backend.gateway.base import ToolGateway
from backend.models import Denial, TaskContext, UpstreamError

AGENT_ID = "prguard-asker"


async def run(ctx: TaskContext, context: dict[str, Any], options: ctxmod.Options,
              gateway: ToolGateway) -> tuple[str, list[str], dict[str, Any]]:
    question = options.question.strip()[:1000]
    if not question:
        raise ValueError("/ask needs a question, e.g. /ask is this change backwards compatible?")
    usage: dict[str, Any] = {}
    if settings.llm_is_mock:
        answer = f"[MOCK LLM] This PR changes {len(context['files'])} file(s). No model is configured to answer."
    else:
        user = ctxmod.pr_prompt(context) + f"\n\n<question>\n{prompts.defang(question)}\n</question>"
        result = await llm.chat(prompts.ASK, user)
        usage = result.usage()
        data = extract_json(result.text)
        answer = str(data.get("answer")) if isinstance(data, dict) and data.get("answer") else result.text.strip()
        if not answer:
            raise llm.LLMError("model returned an empty answer")

    posted: list[str] = []
    if options.publish:
        quoted = "\n".join(f"> {line}" for line in question.splitlines())
        body = f"{quoted}\n\n{answer[:6000]}\n\n<sub>PRGuard /ask · `{ctx.head_sha[:7]}`</sub>"
        try:
            res = await gateway.call(AGENT_ID, ctx, "create_issue_comment",
                                     {"repo": ctx.repo_full_name, "pull_number": ctx.pr_number, "body": body})
            if not isinstance(res, Denial) and isinstance(res.data, dict) and res.data.get("html_url"):
                posted.append(res.data["html_url"])
        except UpstreamError:
            pass
    return answer, posted, usage
