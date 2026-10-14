# PRGuard

A self-hosted AI pull-request reviewer for your CI/CD pipeline. It runs entirely
on your own hardware (llama.cpp, Ollama or vLLM) and stays safe when a pull
request turns hostile.

- **`/review`**: four specialist reviewers (security, quality, tests, docs) plus a
  CI fixer, an independent checker model, and a **pass/fail gate** for the pipeline.
- **`/describe`**: writes the PR summary and a per-file walkthrough into the PR description.
- **`/improve`**: posts concrete code suggestions inline, applied with one click.
- **`/ask <question>`**: answers questions about the PR in a comment.
- **Reports**: JSON, SARIF (GitHub code scanning) and Markdown (the job summary).

It runs as a Docker image, either as a step in any CI system (`prguard review …`)
or as a webhook server with a live dashboard (`prguard serve`).

## How it works

```
GitHub event / CI job
   │
   ▼
prguard ──► .prguard.toml (read from the BASE branch only)
   │
   ├─ build context: PR, diff (numbered, fitted to the model's context), checks*
   │
   ├─ security │ quality │ tests │ docs │ ci-fixer      ← local model (e.g. Llama 3.1 8B)
   ├─ aggregate: merge duplicates, score, verdict      ← plain code
   ├─ checker: confirm / downgrade / reject            ← second model (e.g. Gemma 4 12B)
   ├─ gate: fail the job on HIGH+ findings             ← plain code
   └─ publish: one persistent comment, updated on every push
         │
         ▼
   POLICY GATE ── every GitHub call, default deny ──► GitHub REST (token lives only here)

* checks = your pipeline's own test output (--checks-file), or the PR's linters/tests
  run in a container with no network, no host env, read-only filesystem, non-root.
```

Three things keep a hostile PR contained:

1. **PR code never runs next to secrets.** Either PRGuard reads your pipeline's test
   output, or it runs the PR's linters and tests in a locked container (no network,
   no environment, read-only, no capabilities, non-root). If Docker is missing, PR
   code is not run at all.
2. **Agents never hold the token.** Every GitHub call goes through a policy gate
   (`backend/policies/prguard.policy.yaml`). Each agent may only touch this repo,
   this PR and this commit, and only with its own tools. Merge, push, file writes and
   changing a PR's title or base are never allowed, and the code for them does not exist.
3. **Verdicts and the gate are plain code.** PR text can try to argue with a model,
   but not with an `if` statement. Text aimed at AI reviewers is reported as a
   `prompt-injection` finding and always sends the PR to a human.

## What came from Qodo PR-Agent, and what is PRGuard's own

| | Qodo PR-Agent | PRGuard |
|---|---|---|
| `/describe`, `/improve` (inline suggestions), `/ask`, comment commands | ✓ | ✓ (modelled on PR-Agent) |
| Persistent comment updated on new commits | ✓ | ✓ |
| Repo config file read from the default branch | ✓ | ✓ (`.prguard.toml`, base branch only) |
| Large-PR handling: numbered hunks, skip lock/generated files, fit to context | ✓ | ✓ |
| Multi-agent review + independent checker model | – | ✓ |
| CI gate with exit code, SARIF, job summary, annotations | – | ✓ |
| CI fixer (explains failed CI, re-runs flaky jobs) | – | ✓ |
| Policy gate on every GitHub call, no token in agents | – | ✓ |
| Sandboxed linters/tests for the PR | – | ✓ |
| Comment commands cannot change settings (`/review --api_base=…` is ignored) | denylist | ✓ (no arguments at all) |
| Suggestions checked by code before posting (lines exist, code matches, in diff) | partly | ✓ |
| Model-quality eval on labelled PRs (`prguard eval`) | – | ✓ |
| Git hosts | GitHub, GitLab, Bitbucket, Azure DevOps, Gitea… | GitHub and GitHub Enterprise Server |

## 1. Run your models

Any OpenAI-compatible server works. For a single 16 GB GPU (RTX 4080 Super), two
llama.cpp servers keep reviewer and checker separate:

```bash
# reviewer
llama-server -m Llama-3.1-8B-Instruct-Q4_K_M.gguf --port 8100 -c 16384 -np 2 --jinja
# checker (another model family, so it is a real second opinion)
llama-server -m gemma-4-12b-it-Q4_K_M.gguf --port 8101 -c 8192 -np 1 --jinja
```

`LLM_CONTEXT_TOKENS` should be the per-request context: `-c` divided by `-np`
(here 8192). With Ollama, one server can serve both models
(`LLM_BASE_URL=http://host:11434/v1`). If the GPU is shared with other users,
keep `LLM_CONCURRENCY` at 1–2.

## 2. Build the images

```bash
docker build -t prguard:latest .
docker build -t prguard-sandbox backend/sandbox     # only for --sandbox / server mode
```

Check everything is reachable before wiring it into a pipeline:

```bash
docker run --rm --network host --env-file .env prguard:latest doctor
```

## 3a. Use it in CI (any CI system)

```bash
docker run --rm --network host --user "$(id -u):$(id -g)" \
  -e GITHUB_TOKEN -e LLM_BASE_URL -e LLM_MODEL -e CHECKER_BASE_URL -e CHECKER_MODEL \
  -v "$PWD/prguard-out:/out" \
  prguard:latest review --repo your-org/your-repo --pr 123 --out /out
```

| Exit code | Meaning |
|---|---|
| 0 | passed (or a non-gating command such as describe) |
| 1 | the gate failed: a finding at `fail_on` severity or above survived the checker |
| 2 | usage or setup error (bad arguments, no token, PR not found) |

If the model server is down, the job passes with a warning by default
(`fail_on_error = false`), so a GPU outage never blocks every merge. The report
says the review did not run; it never reports "clean".

Useful flags:

- `--checks-file test-output.txt`: give the reviewers your pipeline's own test or lint output. This is safer and faster than `--sandbox`.
- `--ci-log failed-step.log`: the CI fixer explains this failure.
- `--sandbox`: run the PR's linters and tests in the locked container.
- `--no-publish`: dry run, reports only.
- `--fail-on MEDIUM|HIGH|none`: override the gate threshold.

Ready-made pipelines:

- **GitHub Actions**: [`examples/github-actions/prguard.yml`](examples/github-actions/prguard.yml).
  It runs on PR open and push plus `/commands` in comments, uploads SARIF, and writes
  the job summary. Make the **PRGuard** check *required* in branch protection to
  enforce the gate.
- **Jenkins**: [`examples/jenkins/Jenkinsfile`](examples/jenkins/Jenkinsfile).

In GitHub Actions, `prguard run` reads `$GITHUB_EVENT_PATH` and picks the
commands itself. The job then fails only when a review's gate fails.

## 3b. Or run the server (webhooks and dashboard)

```bash
cp .env.example .env            # GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET, PRGUARD_API_TOKEN, LLM_*
sudo mkdir -p /var/lib/prguard/runs && sudo chown 10001 /var/lib/prguard/runs
DOCKER_GID=$(getent group docker | cut -d: -f3) docker compose up -d --build
```

Point a GitHub App or repo webhook at `https://<host>/webhook`, with events **Pull requests** and
**Issue comments**, content type JSON, and the same secret. The dashboard is at `http://<host>:8000/`.
It shows every GitHub call the agents make (allowed or denied), the review, recent jobs, and
**Human approve** for reviews that need a person.

The compose file mounts the Docker socket so PRGuard can start the sandbox container.
That access is root-equivalent on the host, so use a dedicated machine, or set `PRGUARD_SANDBOX=0` and
drop the mount. Set `PRGUARD_API_TOKEN` whenever the port is reachable by anyone else.

## Configure per repository

Put a `.prguard.toml` on the default branch. Every key is optional. See
[`examples/prguard.toml.example`](examples/prguard.toml.example):

```toml
[gate]
fail_on = "HIGH"          # CRITICAL | HIGH | MEDIUM | LOW | INFO | none
min_confidence = 0.6

[review]
specialists = ["security", "quality"]
extra_instructions = "We use SQLAlchemy 2.x; flag raw SQL string formatting."

[auto]
on_open = ["describe", "review", "improve"]
on_push = ["review"]
```

## Pick models with data: `prguard eval`

```bash
LLM_MODEL=llama-3.1-8b CHECKER_MODEL=gemma-4-12b prguard eval --runs 3
LLM_MODEL=gemma-4-12b  CHECKER_MODEL=llama-3.1-8b prguard eval --runs 3
```

This runs full reviews on the labelled PRs in `demo_target/prs/` (offline, nothing is
posted) and reports recall, noise and time. To grow the benchmark, add your own
real past PRs there with an `"expected"` list in `PR.json`.

## Develop

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                       # offline: mock GitHub + mock model
.venv/bin/python -m backend.cli review --mock --pr 2  # try a command offline
npm --prefix frontend install && npm --prefix frontend run dev   # dashboard on :5173
```

## Limits, stated plainly

- GitHub and GitHub Enterprise Server only; there are no GitLab or Bitbucket providers yet.
- 8B models are noisy. That is why the checker exists, why the gate uses
  `min_confidence`, and why `/improve` suggestions are validated by code. Measure with `prguard eval`.
- On PRs from forks, GitHub Actions gives a read-only token. The gate and reports still work,
  but comments are not posted.
- The sandbox's default checks are Python (pylint and pytest). Set `[checks] command` and `image` for other stacks.
