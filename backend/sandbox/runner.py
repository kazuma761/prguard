"""Run a PR's linters and tests in a locked container.

The container gets: no network, no environment from the host, a read-only
root filesystem, no capabilities, a non-root user, and CPU/memory/PID limits.
If Docker or the image is unavailable, PR code is NOT run at all (fail closed);
PRGuard never falls back to running PR code on the host.

The command comes from the repo's .prguard.toml on the BASE branch, so a PR
cannot change what runs.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.events import bus
from backend.models import Event, TaskContext
from backend.repo_config import ChecksCfg

PROFILE = {"isolation": "docker", "network": "none", "host_env_passed": 0, "filesystem": "read-only",
           "user": "nobody", "caps": "none"}


def host_path(src: Path) -> Path:
    """Path Docker should bind-mount. Differs when PRGuard itself runs in a container."""
    src = src.resolve()
    if settings.sandbox_host_runs_dir:
        try:
            return Path(settings.sandbox_host_runs_dir) / src.relative_to(settings.runs_dir.resolve())
        except ValueError:
            pass
    return src


def docker_command(src: Path, name: str, command: str, image: str) -> list[str]:
    # Deliberately no -e / --env-file: nothing from the host environment goes in.
    return [
        "docker", "run", "--rm", "--name", name,
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,size=128m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "256", "--memory", "1g", "--cpus", "1",
        "--user", "65534:65534",
        "-v", f"{host_path(src)}:/src:ro", "-w", "/src",
        image, "sh", "-c", command,
    ]


async def docker_ready(image: str | None = None) -> str | None:
    """Return None if the sandbox can run, else a short reason."""
    image = image or settings.sandbox_image
    if not shutil.which("docker"):
        return "docker CLI is not installed"
    proc = await asyncio.create_subprocess_exec(
        "docker", "image", "inspect", image, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode == 0:
        return None
    text = err.decode(errors="replace").lower()
    if "no such image" in text:
        return f"sandbox image {image} is not built"
    return "docker daemon is not reachable"


async def _run(cmd: list[str], kill: list[str]) -> tuple[str, str, bool]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=settings.sandbox_timeout_s)
        return out.decode(errors="replace"), err.decode(errors="replace"), False
    except asyncio.TimeoutError:
        proc.kill()
        killer = await asyncio.create_subprocess_exec(*kill, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await killer.wait()
        return "", f"timed out after {settings.sandbox_timeout_s}s", True


def _exits(text: str) -> dict[str, int]:
    return {m.group(1).lower(): int(m.group(2)) for m in re.finditer(r"__(\w+?)_EXIT=(\d+)", text)}


def skipped(reason: str) -> dict[str, Any]:
    return {"ran": False, "source": "none", "reason": reason, "exit_codes": {}, "stdout_tail": "", "stderr_tail": "", "duration_ms": 0}


def from_file(path: Path) -> dict[str, Any]:
    """Use the pipeline's own lint/test output instead of re-running PR code."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return {"ran": True, "source": f"pipeline output ({path.name})", "exit_codes": _exits(text),
            "stdout_tail": text[-4000:], "stderr_tail": "", "duration_ms": 0}


async def run_checks(src: Path, ctx: TaskContext, checks: ChecksCfg) -> dict[str, Any]:
    common = dict(review_id=ctx.review_id, source="sandbox")
    if not checks.command.strip():
        return skipped("no checks configured ([checks] command is empty)")
    image = checks.image or settings.sandbox_image
    problem = await docker_ready(image)
    if problem:
        bus.emit(Event(type="sandbox", payload={"status": "unavailable", "reason": problem}, **common))
        return skipped(f"sandbox unavailable ({problem}); PR code was not run")

    started = time.monotonic()
    bus.emit(Event(type="sandbox", payload={"status": "started", **PROFILE, "image": image}, **common))
    name = f"prguard-{ctx.review_id}"
    out, err, timed_out = await _run(docker_command(src, name, checks.command, image), ["docker", "kill", name])
    result = {
        "ran": True,
        "source": "sandbox",
        "timed_out": timed_out,
        "exit_codes": _exits(out),
        "stdout_tail": re.sub(r"__\w+_EXIT=\d+\n?", "", out)[-4000:],
        "stderr_tail": err[-1500:],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    bus.emit(Event(type="sandbox", payload={"status": "finished", **PROFILE, "exit_codes": result["exit_codes"],
                                           "duration_ms": result["duration_ms"], "timed_out": timed_out}, **common))
    return result
