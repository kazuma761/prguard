"""One chat function for every model call PRGuard makes.

Talks to any OpenAI-compatible server: llama.cpp (`llama-server`), Ollama
(`/v1`), vLLM, LM Studio. Nothing leaves your network.

- A process-wide semaphore caps concurrent requests (LLM_CONCURRENCY), so a
  review never floods a GPU that other people share.
- JSON mode is requested by default and dropped automatically if the server
  refuses it.
- `<think>…</think>` blocks from reasoning models are stripped.
- Tool calls are supported but off by default (LLM_TOOLS=1 to enable).

This module never touches GitHub: tool calls are executed by the caller's
handler, which goes through the policy gateway.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.config import settings

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[str]]
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_semaphores: dict[int, asyncio.Semaphore] = {}


class LLMError(Exception):
    pass


@dataclass
class ChatResult:
    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    cached_calls: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def usage(self) -> dict[str, Any]:
        return {"model": self.model, "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "calls": self.calls, "cached_calls": self.cached_calls, "seconds": round(self.seconds, 2),
                "notes": self.notes}


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    api_key: str
    model: str


def reviewer_endpoint() -> Endpoint:
    return Endpoint(settings.llm_base_url, settings.llm_api_key, settings.llm_model)


def checker_endpoint() -> Endpoint | None:
    if settings.checker is None:
        return None
    base, key, model = settings.checker
    return Endpoint(base, key, model)


def _semaphore() -> asyncio.Semaphore:
    loop = id(asyncio.get_running_loop())
    if loop not in _semaphores:
        _semaphores[loop] = asyncio.Semaphore(max(1, settings.llm_concurrency))
    return _semaphores[loop]


# ------------------------------------------------------------------ cache

def _cache_path(endpoint: Endpoint, body: dict[str, Any]):
    key = json.dumps({"url": endpoint.base_url, **body}, sort_keys=True, default=str)
    return settings.runs_dir / "llm_cache" / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def _cache_get(endpoint: Endpoint, body: dict[str, Any]) -> dict[str, Any] | None:
    if not settings.llm_cache:
        return None
    path = _cache_path(endpoint, body)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except ValueError:
        return None


def _cache_put(endpoint: Endpoint, body: dict[str, Any], data: dict[str, Any]) -> None:
    if settings.llm_cache:
        path = _cache_path(endpoint, body)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")


# --------------------------------------------------------------- transport

def _error_text(data: Any) -> str:
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        return str(err.get("message") if isinstance(err, dict) else err)
    return ""


async def _request(endpoint: Endpoint, body: dict[str, Any], result: ChatResult) -> dict[str, Any]:
    cached = _cache_get(endpoint, body)
    if cached is not None:
        result.calls += 1
        result.cached_calls += 1
        return cached

    headers = {"Authorization": f"Bearer {endpoint.api_key or 'local'}"}
    url = endpoint.base_url + "/chat/completions"
    last = ""
    loop = asyncio.get_running_loop()
    async with _semaphore():
        started = loop.time()
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.llm_timeout_s, connect=10)) as client:
            for attempt in range(3):
                try:
                    response = await client.post(url, json=body, headers=headers)
                except httpx.HTTPError as exc:
                    last = f"cannot reach {endpoint.base_url}: {type(exc).__name__}"
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                error = _error_text(data)
                if response.status_code == 200 and not error and data.get("choices"):
                    result.calls += 1
                    result.seconds += loop.time() - started
                    usage = data.get("usage") or {}
                    result.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                    result.completion_tokens += int(usage.get("completion_tokens") or 0)
                    _cache_put(endpoint, body, data)
                    return data
                last = f"HTTP {response.status_code}: {error or response.text[:300]}"
                if response.status_code in (429, 500, 502, 503) and "context" not in last.lower():
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                break
    raise LLMError(last)


def _text_of(content: Any) -> str:
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return _THINK.sub("", content if isinstance(content, str) else "").strip()


async def chat(system: str, user: str, *, endpoint: Endpoint | None = None,
               tools: list[dict[str, Any]] | None = None, on_tool: ToolHandler | None = None,
               max_tool_turns: int = 3, max_tokens: int | None = None, json_mode: bool | None = None,
               temperature: float = 0.1) -> ChatResult:
    """Send one system+user exchange (plus optional tool turns) and return the final text."""
    endpoint = endpoint or reviewer_endpoint()
    if not endpoint.base_url or not endpoint.model:
        raise LLMError("no model configured (set LLM_BASE_URL and LLM_MODEL)")
    result = ChatResult(text="", model=endpoint.model)
    use_json = settings.llm_json_mode if json_mode is None else json_mode
    use_tools = bool(tools and on_tool and settings.llm_tools)
    base: dict[str, Any] = {"model": endpoint.model, "temperature": temperature,
                            "max_tokens": max_tokens or settings.llm_max_output_tokens}
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    wire_tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                     "parameters": t["input_schema"]}} for t in tools or []]

    try:
        for turn in range(max_tool_turns + 1):
            body = {**base, "messages": messages}
            offer_tools = use_tools and turn < max_tool_turns
            if offer_tools:
                body["tools"] = wire_tools
            elif use_json:
                body["response_format"] = {"type": "json_object"}
            try:
                data = await _request(endpoint, body, result)
            except LLMError as exc:
                text = str(exc).lower()
                if "response_format" in body and ("response_format" in text or "json" in text or "grammar" in text):
                    result.notes.append("server refused JSON mode; retried without it")
                    use_json = False
                    body.pop("response_format")
                    data = await _request(endpoint, body, result)
                elif "tools" in body and ("tool" in text or "jinja" in text or "function" in text):
                    result.notes.append("server refused tool calls (llama.cpp needs --jinja); ran without them")
                    use_tools = False
                    body.pop("tools")
                    data = await _request(endpoint, body, result)
                else:
                    raise
            result.model = data.get("model") or result.model
            message = data["choices"][0].get("message") or {}
            calls = message.get("tool_calls") or []
            if not calls or not on_tool or not offer_tools:
                result.text = _text_of(message.get("content"))
                return result
            messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
            for call in calls:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                output = await on_tool(str(function.get("name", "")), arguments if isinstance(arguments, dict) else {})
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": output})
    except LLMError:
        raise
    except Exception as exc:  # malformed responses, unexpected shapes
        raise LLMError(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
    return result


def family(model: str) -> str:
    """Model family from a model id, used to warn when maker and checker are the same family."""
    name = model.lower().rsplit("/", 1)[-1]
    for fam in ("llama", "gemma", "qwen", "mistral", "mixtral", "phi", "deepseek", "gpt-oss", "granite", "command"):
        if fam in name:
            return fam
    return re.split(r"[-_:.\d]", name, maxsplit=1)[0]
