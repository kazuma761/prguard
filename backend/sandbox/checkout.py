"""Put the PR head into a local directory for the sandbox, then strip .git.

The token is used only by this git process (as an HTTP header, never written
to disk or into the checkout) and the checkout is mounted read-only into a
container that receives no environment at all.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
from pathlib import Path

from backend.config import settings
from backend.models import TaskContext


async def _git(*args: str, cwd: Path | None = None, header: str = "") -> None:
    extra = ["-c", f"http.extraheader={header}"] if header else []
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", "credential.helper=", *extra, *args,
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_TERMINAL_PROMPT": "0", "HOME": "/nonexistent"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {err.decode(errors='replace')[-300:]}")


async def checkout_pr(ctx: TaskContext, workdir: Path | None = None) -> Path:
    dest = settings.runs_dir / "checkouts" / ctx.review_id
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if workdir is not None:  # CI: the pipeline already checked the PR out
        shutil.copytree(workdir, dest, ignore=shutil.ignore_patterns(".git"), symlinks=True)
        return dest

    if settings.upstream == "mock":
        from backend.gateway import mock_upstream

        mock_upstream().materialize(ctx.pr_number, dest)
        return dest

    header = ""
    if settings.github_token:
        basic = base64.b64encode(f"x-access-token:{settings.github_token}".encode()).decode()
        header = f"AUTHORIZATION: basic {basic}"
    url = f"{settings.github_server_url}/{ctx.repo_full_name}.git"
    await _git("init", "--quiet", str(dest))
    try:
        await _git("fetch", "--depth", "1", "--quiet", url, ctx.head_sha, cwd=dest, header=header)
    except RuntimeError:  # servers that refuse fetching a bare SHA
        await _git("fetch", "--depth", "1", "--quiet", url, f"pull/{ctx.pr_number}/head", cwd=dest, header=header)
    await _git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=dest)
    shutil.rmtree(dest / ".git", ignore_errors=True)
    return dest
