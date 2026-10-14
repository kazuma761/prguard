"""`prguard doctor`: check every dependency before wiring PRGuard into a pipeline."""

from __future__ import annotations

import time

import httpx

from backend.agents import llm
from backend.config import settings
from backend.sandbox.runner import docker_ready


async def _model(label: str, endpoint: llm.Endpoint | None) -> bool:
    if endpoint is None or not endpoint.base_url:
        print(f"  {label:9} not configured")
        return label != "reviewer"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(endpoint.base_url + "/models", headers={"Authorization": f"Bearer {endpoint.api_key or 'local'}"})
        served = [m.get("id") for m in (res.json().get("data") or [])] if res.status_code == 200 else []
    except (httpx.HTTPError, ValueError) as exc:
        print(f"  {label:9} ✗ cannot reach {endpoint.base_url} ({type(exc).__name__})")
        return False
    note = "" if not served or endpoint.model in served else f" (server lists {served}; llama.cpp serves one model under any name)"
    started = time.monotonic()
    try:
        result = await llm.chat('Reply with JSON {"ok": true}.', "ping", endpoint=endpoint, max_tokens=20)
    except llm.LLMError as exc:
        print(f"  {label:9} ✗ {endpoint.model} at {endpoint.base_url}: {exc}")
        return False
    print(f"  {label:9} ✓ {endpoint.model} at {endpoint.base_url} answered in {time.monotonic() - started:.1f}s"
          f" ({result.text[:40]!r}){note}{' · ' + '; '.join(result.notes) if result.notes else ''}")
    return True


async def doctor() -> int:
    print("PRGuard doctor")
    print("models:")
    ok = await _model("reviewer", llm.reviewer_endpoint() if settings.llm_base_url else None)
    ok &= await _model("checker", llm.checker_endpoint())
    print(f"  context  {settings.llm_context_tokens} tokens per request, {settings.llm_concurrency} concurrent, "
          f"JSON mode {'on' if settings.llm_json_mode else 'off'}, tools {'on' if settings.llm_tools else 'off'}")

    print("github:")
    if not settings.github_token:
        print(f"  ✗ GITHUB_TOKEN is not set ({settings.github_api_url})")
        ok = False
    else:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.get(settings.github_api_url + "/rate_limit",
                                       headers={"Authorization": f"Bearer {settings.github_token}", "User-Agent": "prguard"})
            if res.status_code == 200:
                core = res.json().get("resources", {}).get("core", {})
                print(f"  ✓ token works against {settings.github_api_url} ({core.get('remaining')}/{core.get('limit')} requests left)")
            else:
                print(f"  ✗ {settings.github_api_url} answered {res.status_code}")
                ok = False
        except httpx.HTTPError as exc:
            print(f"  ✗ cannot reach {settings.github_api_url} ({type(exc).__name__})")
            ok = False

    print("sandbox:")
    problem = await docker_ready()
    print(f"  {'✓ ' + settings.sandbox_image + ' ready' if not problem else '– ' + problem + ' (only needed for --sandbox)'}")
    print("result:", "ready" if ok else "not ready")
    return 0 if ok else 2
