"""MockUpstream: offline stand-in for GitHub, built from demo_target/.

Lets the whole pipeline, the tests and `prguard eval` run with no network, no
token and no real repository. Writes are recorded so tests and the UI can show
them.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.models import UpstreamError

class MockUpstream:
    """In-memory GitHub double. Writes are recorded so the UI can show them."""

    name = "MOCK"

    def __init__(self, demo_dir: Path | None = None) -> None:
        self.demo_dir = demo_dir or settings.demo_target_dir
        self.prs: dict[int, dict[str, Any]] = {}
        for meta_path in sorted((self.demo_dir / "prs").glob("*/PR.json")):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["dir"] = meta_path.parent
            self.prs[int(meta["number"])] = meta
        self.reset()

    def reset(self) -> None:
        self.reviews: list[dict[str, Any]] = []
        self.merged: set[int] = set()
        self.writes: list[dict[str, Any]] = []
        self.reruns: list[int] = []
        self.comments: list[dict[str, Any]] = []
        self.bodies: dict[int, str] = {}

    # ---- trees -------------------------------------------------------
    def _base_tree(self) -> dict[str, str]:
        tree: dict[str, str] = {}
        for path in sorted(self.demo_dir.rglob("*")):
            rel = path.relative_to(self.demo_dir).as_posix()
            if path.is_file() and not rel.startswith("prs/") and "__pycache__" not in rel:
                tree[rel] = path.read_text(encoding="utf-8")
        return tree

    def _head_tree(self, number: int) -> dict[str, str]:
        tree = self._base_tree()
        pr_dir: Path = self.prs[number]["dir"]
        for path in sorted(pr_dir.rglob("*")):
            rel = path.relative_to(pr_dir).as_posix()
            if path.is_file() and rel not in ("PR.json", "ci_log.txt") and "__pycache__" not in rel:
                tree[rel] = path.read_text(encoding="utf-8")
        return tree

    def _run(self, number: int) -> dict[str, Any]:
        """The CI run for a PR's head commit, as the mock GitHub Actions would report it."""
        ci = self.prs[number].get("ci") or {}
        return {
            "id": 9000 + number, "name": "ci", "head_sha": self.head_sha(number),
            "head_branch": self.prs[number]["branch"], "status": "completed",
            "conclusion": ci.get("conclusion", "success"), "job": ci.get("job", "test"),
            "html_url": f"https://github.com/{settings.target_repo}/actions/runs/{9000 + number}",
        }

    def head_sha(self, number: int) -> str:
        digest = hashlib.sha1()
        for rel, text in sorted(self._head_tree(number).items()):
            digest.update(rel.encode() + b"\0" + text.encode())
        return digest.hexdigest()

    def materialize(self, number: int, dest: Path) -> None:
        """Write the PR head tree to `dest` (the mock equivalent of a checkout)."""
        if dest.exists():
            shutil.rmtree(dest)
        for rel, text in self._head_tree(number).items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def _changed(self, number: int) -> list[dict[str, Any]]:
        base, head = self._base_tree(), self._head_tree(number)
        files = []
        for rel in sorted(set(base) | set(head)):
            old, new = base.get(rel, ""), head.get(rel, "")
            if old == new:
                continue
            patch = "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{rel}", f"b/{rel}"
                )
            )
            status = "added" if rel not in base else "removed" if rel not in head else "modified"
            files.append({"filename": rel, "status": status, "patch": patch})
        return files

    # ---- tools -------------------------------------------------------
    def _pr(self, args: dict[str, Any]) -> int:
        number = int(args.get("pull_number", 0))
        if number not in self.prs:
            raise UpstreamError(f"404 Not Found: pull request #{number}")
        return number

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        repo = args.get("repo", settings.target_repo)
        if repo not in (settings.target_repo, settings.other_repo):
            raise UpstreamError(f"404 Not Found: {repo}")
        other = repo == settings.other_repo
        url = f"https://github.com/{repo}"

        if tool == "get_pull_request":
            n = self._pr(args)
            meta = self.prs[n]
            return {
                "number": n,
                "title": meta["title"],
                "body": self.bodies.get(n, meta["body"]),
                "state": "merged" if n in self.merged else "open",
                "head": {"sha": self.head_sha(n), "ref": meta["branch"]},
                "base": {"ref": "main", "sha": "0" * 40},
                "user": {"login": repo.split("/")[0]},
                "html_url": f"{url}/pull/{n}",
            }
        if tool == "get_pull_request_files":
            return self._changed(self._pr(args))
        if tool == "get_pull_request_diff":
            return "".join(f["patch"] for f in self._changed(self._pr(args)))
        if tool == "get_file_contents":
            if other:
                tree = {"README.md": "# prguard-demo-other\n\nSecond throwaway repo.\n"}
            else:
                ref = str(args.get("ref", ""))
                number = next((n for n in self.prs if ref in (self.head_sha(n), self.prs[n]["branch"])), None)
                tree = self._head_tree(number) if number else self._base_tree()
            path = str(args.get("path", "")).lstrip("/")
            if path not in tree:
                raise UpstreamError(f"404 Not Found: {path}")
            return {"path": path, "content": tree[path]}
        if tool == "search_code":
            query = str(args.get("query", "")).lower()
            hits = [
                {"path": rel, "line": i + 1, "text": line.strip()}
                for rel, text in self._base_tree().items()
                for i, line in enumerate(text.splitlines())
                if query and query in line.lower()
            ]
            return {"total_count": len(hits), "items": hits[:20]}
        if tool == "create_pull_request_review":
            n = int(args.get("pull_number", 0))
            review = {
                "id": len(self.reviews) + 1,
                "repo": repo,
                "pull_number": n,
                "event": args.get("event", "COMMENT"),
                "body": args.get("body", ""),
                "commit_id": args.get("commit_id", ""),
                "comments": args.get("comments", []),
                "html_url": f"{url}/pull/{n}#pullrequestreview-{len(self.reviews) + 1}",
            }
            self.reviews.append(review)
            return review
        if tool in ("upsert_issue_comment", "create_issue_comment"):
            n = self._pr(args)
            tag = f"<!-- prguard:{args['marker']} -->" if tool == "upsert_issue_comment" else None
            body = f"{tag}\n{args['body']}" if tag else args["body"]
            existing = next((c for c in self.comments if tag and c["pull_number"] == n and tag in c["body"]), None)
            if existing:
                existing["body"] = body
                existing["updates"] += 1
                return {"id": existing["id"], "html_url": existing["html_url"], "updated": True}
            comment = {"id": len(self.comments) + 1, "pull_number": n, "body": body, "updates": 0,
                       "html_url": f"{url}/pull/{n}#issuecomment-{len(self.comments) + 1}"}
            self.comments.append(comment)
            return {"id": comment["id"], "html_url": comment["html_url"], "updated": False}
        if tool == "update_pull_request_body":
            n = self._pr(args)
            self.bodies[n] = args["body"]
            return {"number": n, "html_url": f"{url}/pull/{n}"}
        if tool == "merge_pull_request":
            n = int(args.get("pull_number", 0))
            self.merged.add(n)
            self.writes.append({"tool": tool, "repo": repo, "pull_number": n})
            return {"merged": True, "message": f"Pull request #{n} merged (mock)"}
        if tool in ("push_files", "create_or_update_file", "delete_file"):
            self.writes.append({"tool": tool, **{k: v for k, v in args.items() if k != "files"}})
            return {"ok": True, "message": f"{tool} applied to {repo} (mock)"}
        if tool == "list_workflow_runs":
            runs = [] if other else [self._run(n) for n in sorted(self.prs)]
            return {"total_count": len(runs), "workflow_runs": runs}
        if tool in ("get_job_logs", "rerun_failed_jobs"):
            number = int(args.get("run_id", 0)) - 9000
            if other or number not in self.prs:
                raise UpstreamError(f"404 Not Found: workflow run {args.get('run_id')}")
            if tool == "rerun_failed_jobs":
                self.reruns.append(number)
                return {"ok": True, "message": f"re-running failed jobs of run {9000 + number} (mock)"}
            run = self._run(number)
            log_path: Path = self.prs[number]["dir"] / "ci_log.txt"
            logs = log_path.read_text(encoding="utf-8") if log_path.exists() else "All checks passed.\n"
            return {"run_id": run["id"], "jobs": [{"name": run["job"], "conclusion": run["conclusion"], "logs": logs}]}
        raise UpstreamError(f"unknown tool {tool}")
