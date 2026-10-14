"""GitHubUpstream: the GitHub REST calls PRGuard can make, and nothing else.

The token lives only here. Agents reach these functions solely through the
PolicyGateway, which checks every call against prguard.policy.yaml first.

Deliberately NOT implemented: merge, push, file write/delete, changing a PR's
title, state or base. The policy denies them anyway; not having the code at all
is the second lock.

Works with github.com and GitHub Enterprise Server (set GITHUB_API_URL).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from backend.config import settings
from backend.models import UpstreamError

log = logging.getLogger("prguard.github")

MAX_FILES = 300          # GitHub's PR files API stops at 3000; reviews of more are not useful
MAX_COMMENT_PAGES = 10   # look for our persistent comment in the first 1000 comments


def marker_tag(marker: str) -> str:
    return f"<!-- prguard:{marker} -->"


class GitHubUpstream:
    name = "GITHUB"

    def __init__(self, token: str | None = None, api_url: str | None = None) -> None:
        self.token = token if token is not None else settings.github_token
        self.api = (api_url or settings.github_api_url).rstrip("/")
        self._login: str | None = None

    # ------------------------------------------------------------ plumbing
    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "prguard"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(self, method: str, path: str, *, accept: str = "application/vnd.github+json",
                       **kwargs: Any) -> httpx.Response:
        last = ""
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=True) as client:
            for attempt in range(3):
                try:
                    response = await client.request(method, self.api + path, headers=self._headers(accept), **kwargs)
                except httpx.HTTPError as exc:
                    last = f"network error: {type(exc).__name__}"
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if response.status_code in (502, 503, 504) or (
                        response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
                    last = f"GitHub {response.status_code}"
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                if response.status_code >= 400:
                    try:
                        message = response.json().get("message", response.text[:200])
                    except ValueError:
                        message = response.text[:200]
                    raise UpstreamError(f"GitHub {response.status_code}: {message}")
                return response
        raise UpstreamError(last or "GitHub request failed")

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        return response.json() if response.content else {}

    async def _login_name(self) -> str:
        """Who PRGuard posts as. Installation and Actions tokens cannot call /user."""
        if self._login is None:
            try:
                self._login = str((await self._json("GET", "/user")).get("login") or settings.github_bot_login)
            except UpstreamError:
                self._login = settings.github_bot_login
        return self._login

    @staticmethod
    def _split(repo: str) -> tuple[str, str]:
        owner, _, name = str(repo).partition("/")
        if not owner or not name:
            raise UpstreamError(f"bad repo name {repo!r}")
        return owner, name

    # ---------------------------------------------------------------- tools
    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        handler = getattr(self, f"_t_{tool}", None)
        if handler is None:
            raise UpstreamError(f"PRGuard has no GitHub tool named {tool}")
        owner, repo = self._split(args.get("repo", ""))
        return await handler(owner, repo, args)

    async def _t_get_pull_request(self, owner: str, repo: str, a: dict[str, Any]) -> dict:
        pr = await self._json("GET", f"/repos/{owner}/{repo}/pulls/{int(a['pull_number'])}")
        return {"number": pr["number"], "title": pr["title"], "body": pr.get("body") or "", "state": pr["state"],
                "draft": pr.get("draft", False), "merged": pr.get("merged", False),
                "head": {"sha": pr["head"]["sha"], "ref": pr["head"]["ref"]},
                "base": {"ref": pr["base"]["ref"], "sha": pr["base"]["sha"]},
                "user": {"login": pr["user"]["login"]}, "html_url": pr["html_url"],
                "labels": [label["name"] for label in pr.get("labels", [])]}

    async def _t_get_pull_request_files(self, owner: str, repo: str, a: dict[str, Any]) -> list[dict]:
        out: list[dict] = []
        for page in range(1, MAX_FILES // 100 + 1):
            files = await self._json("GET", f"/repos/{owner}/{repo}/pulls/{int(a['pull_number'])}/files",
                                     params={"per_page": 100, "page": page})
            out += [{"filename": f["filename"], "status": f["status"], "patch": f.get("patch", ""),
                     "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)} for f in files]
            if len(files) < 100:
                break
        return out

    async def _t_get_pull_request_diff(self, owner: str, repo: str, a: dict[str, Any]) -> str:
        response = await self._request("GET", f"/repos/{owner}/{repo}/pulls/{int(a['pull_number'])}",
                                       accept="application/vnd.github.diff")
        return response.text

    async def _t_get_file_contents(self, owner: str, repo: str, a: dict[str, Any]) -> dict:
        path = str(a.get("path", "")).lstrip("/")
        params = {"ref": a["ref"]} if a.get("ref") else None
        response = await self._request("GET", f"/repos/{owner}/{repo}/contents/{path}", params=params,
                                       accept="application/vnd.github.raw")
        return {"path": path, "content": response.text}

    async def _t_search_code(self, owner: str, repo: str, a: dict[str, Any]) -> dict:
        data = await self._json("GET", "/search/code", params={"q": f"{a.get('query', '')} repo:{owner}/{repo}", "per_page": 20})
        return {"total_count": data.get("total_count", 0), "items": [{"path": i["path"]} for i in data.get("items", [])]}

    async def _t_list_workflow_runs(self, owner: str, repo: str, a: dict[str, Any]) -> dict:
        params: dict[str, Any] = {"per_page": 30}
        if a.get("head_sha"):
            params["head_sha"] = a["head_sha"]
        data = await self._json("GET", f"/repos/{owner}/{repo}/actions/runs", params=params)
        runs = [{"id": r["id"], "name": r["name"], "head_sha": r["head_sha"], "head_branch": r["head_branch"],
                 "status": r["status"], "conclusion": r["conclusion"], "html_url": r["html_url"]}
                for r in data.get("workflow_runs", [])]
        return {"total_count": len(runs), "workflow_runs": runs}

    async def _t_get_job_logs(self, owner: str, repo: str, a: dict[str, Any]) -> dict:
        run_id = int(a["run_id"])
        data = await self._json("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs")
        jobs = []
        for job in data.get("jobs", []):
            entry = {"name": job["name"], "conclusion": job["conclusion"], "logs": ""}
            if job["conclusion"] == "failure":
                try:
                    entry["logs"] = (await self._request("GET", f"/repos/{owner}/{repo}/actions/jobs/{job['id']}/logs")).text[-12000:]
                except UpstreamError as exc:
                    failed = [s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"]
                    entry["logs"] = f"(logs unavailable: {exc}) failed steps: {failed}"
            jobs.append(entry)
        return {"run_id": run_id, "jobs": jobs}

    async def _t_rerun_failed_jobs(self, owner: str, repo: str, a: dict[str, Any]) -> dict:
        await self._json("POST", f"/repos/{owner}/{repo}/actions/runs/{int(a['run_id'])}/rerun-failed-jobs")
        return {"ok": True, "message": f"re-running failed jobs of run {a['run_id']}"}

    async def _t_create_pull_request_review(self, owner: str, repo: str, a: dict[str, Any]) -> dict:
        body: dict[str, Any] = {"event": a["event"], "body": a.get("body", "")}
        if a.get("commit_id"):
            body["commit_id"] = a["commit_id"]
        if a.get("comments"):
            body["comments"] = a["comments"]
        review = await self._json("POST", f"/repos/{owner}/{repo}/pulls/{int(a['pull_number'])}/reviews", json=body)
        return {"id": review["id"], "state": review["state"], "html_url": review["html_url"]}

    async def _t_create_issue_comment(self, owner: str, repo: str, a: dict[str, Any]) -> dict:
        comment = await self._json("POST", f"/repos/{owner}/{repo}/issues/{int(a['pull_number'])}/comments",
                                   json={"body": a["body"]})
        return {"id": comment["id"], "html_url": comment["html_url"]}

    async def _t_upsert_issue_comment(self, owner: str, repo: str, a: dict[str, Any]) -> dict:
        """Edit PRGuard's own comment carrying this marker, or create it.

        Only comments written by PRGuard's own login are edited, so anyone
        pasting the marker into their comment cannot make PRGuard touch it.
        """
        tag = marker_tag(str(a["marker"]))
        body = f"{tag}\n{a['body']}"
        me = (await self._login_name()).lower()
        number = int(a["pull_number"])
        for page in range(1, MAX_COMMENT_PAGES + 1):
            comments = await self._json("GET", f"/repos/{owner}/{repo}/issues/{number}/comments",
                                        params={"per_page": 100, "page": page})
            for comment in comments:
                if tag in (comment.get("body") or "") and str(comment["user"]["login"]).lower() == me:
                    edited = await self._json("PATCH", f"/repos/{owner}/{repo}/issues/comments/{comment['id']}",
                                              json={"body": body})
                    return {"id": edited["id"], "html_url": edited["html_url"], "updated": True}
            if len(comments) < 100:
                break
        created = await self._json("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body})
        return {"id": created["id"], "html_url": created["html_url"], "updated": False}

    async def _t_update_pull_request_body(self, owner: str, repo: str, a: dict[str, Any]) -> dict:
        pr = await self._json("PATCH", f"/repos/{owner}/{repo}/pulls/{int(a['pull_number'])}", json={"body": a["body"]})
        return {"number": pr["number"], "html_url": pr["html_url"]}
