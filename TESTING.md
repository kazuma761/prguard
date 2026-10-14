# PRGuard `local-cicd`: test plan for the target machine

**For the agent running this:** work through the stages in order. Each stage
lists exact commands and what "pass" looks like. Don't skip a failed stage:
fix it, or write down why it failed, before moving on. Record results in the
table at the end. Use a **throwaway GitHub repo** for anything that posts, never
a production repo.

Already verified in the build environment (no need to repeat beyond stage 1):
offline tests, every command against a fake model server, webhook signing and
comment authorisation, and sandbox isolation using a stand-in image.

Never verified, so this plan concentrates on them: the Docker image build,
real models, the GitHub REST write calls against the live API, the GitHub
Actions workflow, server mode behind a real webhook, and private repos / GHES.

---

## Stage 0: Setup

```bash
git clone https://github.com/Shrirajas/prguard && cd prguard && git checkout local-cicd
cp .env.example .env     # fill in GITHUB_TOKEN, LLM_*, CHECKER_*
```

Facts to collect first, because later stages depend on them:

- [ ] llama.cpp launch flags for each model: `-c` (context) and `-np` (parallel slots).
      Set `LLM_CONTEXT_TOKENS` = `-c / -np`. If it is set higher than the real slot size,
      llama.cpp will reject long prompts.
- [ ] Is Gemma served separately (e.g. port 8101), or by the same server? Set `CHECKER_BASE_URL` to match.
- [ ] github.com or GitHub Enterprise Server? For GHES set `GITHUB_API_URL=https://<host>/api/v3`
      and `GITHUB_SERVER_URL=https://<host>`.
- [ ] Docker group id: `getent group docker | cut -d: -f3`.

## Stage 1: Offline sanity (5 min)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                                # expect: 35 passed
.venv/bin/python -m backend.cli review --mock --pr 2; echo "exit=$?"   # expect exit=1, CRITICAL finding
.venv/bin/python -m backend.cli review --mock --pr 1; echo "exit=$?"   # expect exit=0
```

## Stage 2: Docker images (never built before)

```bash
docker build -t prguard:latest .
docker build -t prguard-sandbox backend/sandbox
docker run --rm prguard:latest --help
docker run --rm prguard:latest review --mock --pr 2 --out /out; echo "exit=$?"    # expect 1
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/out:/out" prguard:latest review --mock --pr 1 --out /out
ls out/   # prguard-report.json  prguard.sarif  prguard-summary.md
```

Pass when:

- [ ] Both images build.
- [ ] The container runs as an arbitrary uid and can still write `/out` and its runs dir.

## Stage 3: Model servers

```bash
docker run --rm --network host --env-file .env prguard:latest doctor
```

Pass when:

- [ ] Reviewer ✓ and checker ✓, each answering in well under `LLM_TIMEOUT_S`.
- [ ] No note saying "server refused JSON mode". If you see it, PRGuard still works,
      but JSON parsing gets less reliable. Check the llama.cpp version.
- [ ] Leave `LLM_TOOLS=0` unless llama.cpp runs with `--jinja` and you want to try tool use.

## Stage 4: Real-model quality (the most important stage)

```bash
docker run --rm --network host --env-file .env -v "$PWD/out:/out" prguard:latest eval --runs 3 --out /out
# Swap roles to compare:
docker run --rm --network host --env-file .env -e LLM_MODEL=gemma-4-12b -e CHECKER_MODEL=llama-3.1-8b \
  -e LLM_BASE_URL=<gemma url> -e CHECKER_BASE_URL=<llama url> -v "$PWD/out2:/out" prguard:latest eval --runs 3 --out /out
```

Record overall recall, noise per PR, and seconds per PR for each pairing.

Must pass on the hostile PR #2:

- [ ] `code-execution` (the `.pylintrc` that loads PR code) found in at least 2 of 3 runs.
- [ ] `prompt-injection` (the comment addressing AI reviewers) found in at least 2 of 3 runs.
- [ ] Verdict `NEEDS_HUMAN` or the gate failed.
- [ ] No run crashes. A run with every reviewer failed means a timeout or context overflow.
      Check the llama.cpp logs and `NODE_TIMEOUT_S`.

If recall is poor:

1. Try `LLM_CONTEXT_TOKENS` closer to the real slot size.
2. Try fewer specialists in `.prguard.toml`.
3. Try a code-tuned model.
4. Then tune the prompts in `backend/agents/prompts.py`.

Re-run `eval` after every change and keep the JSON files to compare.

## Stage 5: Sandbox with the real image

```bash
python3 -c "import http.server as h; h.HTTPServer(('0.0.0.0',9999),h.BaseHTTPRequestHandler).serve_forever()" &
.venv/bin/python -m backend.cli review --mock --pr 2 --sandbox --no-publish --json --out out-sbx > sbx.json
grep -o '"exit_codes": {[^}]*}' sbx.json   # pylint and pytest both report exit codes
kill %1
```

Pass when:

- [ ] Both checks ran (exit codes present).
- [ ] The listener on :9999 received nothing.

## Stage 6: Live GitHub on a throwaway repo

Create a test repo, e.g. `prguard-test`, with a small Python project. Open 3 PRs:

1. A small bug, for example a missing negative-amount check.
2. A copy of `demo_target/prs/02_hostile`.
3. A PR touching 150+ files or a big lockfile.

Token permissions: contents:read, pull-requests:write, issues:write, actions:read.

```bash
R="--repo <you>/prguard-test"
docker run --rm --network host --env-file .env prguard:latest review   $R --pr 1 --no-publish   # dry run first
docker run --rm --network host --env-file .env prguard:latest review   $R --pr 1
git -C <test repo> commit --allow-empty -m nudge && git push           # then run review again
docker run --rm --network host --env-file .env prguard:latest describe $R --pr 1
docker run --rm --network host --env-file .env prguard:latest describe $R --pr 1   # second run
docker run --rm --network host --env-file .env prguard:latest improve  $R --pr 1
docker run --rm --network host --env-file .env prguard:latest ask      $R --pr 1 "is this backwards compatible?"
docker run --rm --network host --env-file .env prguard:latest review   $R --pr 3 --no-publish
```

These GitHub write calls have never touched the live API. Check each one on GitHub:

- [ ] **review:** exactly one PRGuard comment. After the push and the second run, the same
      comment is edited, not duplicated. If it is duplicated, the bot login detection failed:
      set `GITHUB_BOT_LOGIN` to the account the token belongs to. For a GitHub App, that is `<app-slug>[bot]`.
- [ ] **describe:** a PRGuard section appears in the PR description and the author's text is
      untouched. The second run replaces the section rather than appending a new one.
- [ ] **improve:** suggestions sit on the right lines, and **Commit suggestion** applies cleanly.
      Watch multi-line suggestions: GitHub rejects a range spanning two diff hunks. If the whole
      review POST fails with HTTP 422, note the response; the fix is in `backend/agents/improver.py`.
- [ ] **ask:** an answer comment quoting the question.
- [ ] **large PR:** the report notes skipped or omitted files, and llama.cpp shows no
      "context exceeded" errors.
- [ ] **private repo:** run `--sandbox` once on a private repo. The checkout uses the token as
      an HTTP header and should succeed. On GHES, check that fetching a bare SHA works; otherwise
      it falls back to `pull/N/head`.

## Stage 7: Security checks on the live repo

- [ ] A comment `/review` from an account that is **not** a collaborator is ignored.
      The `prguard run` output names the reason.
- [ ] `/review --openai.api_base=http://evil` runs a normal review; the arguments have no effect.
- [ ] A PR that adds a `.prguard.toml` with `[gate] fail_on = "none"` is still gated by the
      **base branch** config.
- [ ] Another user posts a comment containing `<!-- prguard:review -->`. PRGuard must not edit
      their comment; it edits or creates its own.
- [ ] Nothing ever merges, pushes or approves. APPROVE only happens through **Human approve**
      in the dashboard.

## Stage 8: GitHub Actions workflow (self-hosted runner)

1. Copy `examples/github-actions/prguard.yml` to `.github/workflows/` in the test repo.
2. Set repo variables `PRGUARD_LLM_BASE_URL`, `PRGUARD_LLM_MODEL`, `PRGUARD_CHECKER_*`.
3. Build `prguard:latest` on the runner.

Pass when:

- [ ] Opening a PR runs describe + review. The job fails on PR #2 and passes on PR #1.
- [ ] Annotations appear on the diff, and the job summary shows the review.
- [ ] The `prguard-…` artifact contains the reports.
- [ ] SARIF upload works, or skips gracefully if code scanning is off for the repo.
- [ ] Commenting `/improve` triggers a run on the PR.
- [ ] With **PRGuard** marked as a required check in branch protection, PR #2 cannot be merged.
- [ ] Pushing twice quickly cancels the first run (the workflow's concurrency group).

## Stage 9: Server mode (webhook + dashboard)

```bash
sudo mkdir -p /var/lib/prguard/runs && sudo chown 10001 /var/lib/prguard/runs
DOCKER_GID=$(getent group docker | cut -d: -f3) docker compose up -d --build
docker compose logs -f prguard
```

Point a repo webhook (events: Pull requests, Issue comments; JSON; secret set) at a URL
GitHub can reach: an internal hostname for GHES, or a tunnel for github.com.

Pass when:

- [ ] GitHub's webhook "Recent deliveries" shows 200 responses, and a ping returns `pong`.
- [ ] Opening a PR runs the `on_open` commands, and the dashboard shows the calls live.
- [ ] The sandbox runs from inside the container (sibling container). Its status in the dashboard
      is `finished`, not `unavailable`. If it says checkout path not found, check
      `SANDBOX_HOST_RUNS_DIR` matches the host path.
- [ ] With `PRGUARD_API_TOKEN` set: the dashboard asks for the token (🔑), `/jobs` without it returns 401.
- [ ] Human approve on a NEEDS_HUMAN review posts an APPROVE. GitHub refuses when the token's
      account authored the PR; that is expected.

## Stage 10: Failure modes

- [ ] **Model server stopped.** `review` exits 0 with "could not finish" in the gate reason
      (`fail_on_error = false`). With `fail_on_error = true` in `.prguard.toml`, it exits 1.
- [ ] **GPU busy with other users.** Review time stays acceptable with `LLM_CONCURRENCY=1`,
      and other users' chats keep working. Record the time.
- [ ] **Bad token.** `review` exits 2 with a clear message, not a stack trace.
- [ ] **Invalid `.prguard.toml`.** The review runs with defaults, and the warning appears in the comment.

---

## Report back

| Stage | Pass/fail | Notes (errors, timings, numbers) |
|---|---|---|
| 1 Offline | | |
| 2 Docker build | | |
| 3 Doctor | | |
| 4 Eval: Llama reviews / Gemma checks | | recall / noise / s per PR |
| 4 Eval: swapped | | recall / noise / s per PR |
| 5 Sandbox | | |
| 6 Live GitHub (each command) | | |
| 7 Security | | |
| 8 GitHub Actions | | |
| 9 Server mode | | |
| 10 Failure modes | | |

For any failure, include the command, the exit code, the end of the output, and
`prguard-report.json` if one was written.
