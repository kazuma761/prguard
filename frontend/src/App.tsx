import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import FlowGraph from "./FlowGraph";
import { getJobs, getState, latestReview, latestRun, post, setToken, token, useEvents, type Ev, type Job, type Review, type ServerState } from "./store";

const time = (ts: number) => new Date(ts * 1000).toLocaleTimeString("en-GB", { hour12: false });

function argSummary(e: Ev): string {
  const a = e.args_redacted ?? {};
  const repo = typeof a.repo === "string" ? a.repo.split("/")[1] : "";
  const parts = [repo && `repo=${repo}${a.pull_number ? `#${a.pull_number}` : ""}`, a.path && `path=${a.path}`, a.event && `event=${a.event}`, a.branch && `branch=${a.branch}`];
  return parts.filter(Boolean).join(" ");
}

function Panel({ title, right, children, className = "" }: { title: string; right?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={`flex min-h-0 flex-col border border-line bg-panel/70 ${className}`}>
      <header className="flex shrink-0 items-center justify-between gap-2 border-b border-line px-3 py-1.5">
        <h2 className="mr-2 truncate font-mono text-[10.5px] uppercase tracking-[0.18em] text-dim">{title}</h2>
        {right}
      </header>
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </section>
  );
}

function DecisionFeed({ events }: { events: Ev[] }) {
  const [filter, setFilter] = useState<"ALL" | "ALLOW" | "DENY">("ALL");
  const rows = useMemo(
    () => events.filter((e) => (e.type === "decision" && (filter === "ALL" || e.decision === filter)) || (e.type === "error" && e.tool && filter === "ALL")),
    [events, filter],
  );
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [rows.length]);
  const allow = events.filter((e) => e.type === "decision" && e.decision === "ALLOW").length;
  const deny = events.filter((e) => e.type === "decision" && e.decision === "DENY").length;

  return (
    <Panel
      className="h-[340px] lg:h-auto"
      title="Decision feed"
      right={
        <div className="flex gap-1 font-mono text-[10px]">
          {(["ALL", "ALLOW", "DENY"] as const).map((f) => (
            <button key={f} onClick={() => setFilter(f)}
              className={`border px-1.5 py-0.5 ${filter === f ? "border-slate-400 text-slate-100" : "border-line text-dim"}`}>
              {f === "ALL" ? `All ${allow + deny}` : f === "ALLOW" ? `Allow ${allow}` : `Deny ${deny}`}
            </button>
          ))}
        </div>
      }
    >
      <div className="font-mono text-[11px] leading-relaxed">
        {rows.length === 0 && <p className="p-3 text-dim">No GitHub calls yet. Run a command, or wait for a webhook.</p>}
        {rows.map((e, i) => e.type === "error" ? (
          <div key={i} className="type-in border-b border-line/50 px-3 py-1">
            <span className="text-dim">{time(e.ts)}</span> <span className="font-bold text-warn">ERROR</span>{" "}
            <span className="text-sky-300">{e.source}</span> <span className="text-slate-100">{e.tool}</span>{" "}
            <span className="text-dim">{argSummary(e)}</span>
            <div className="pl-[4.6rem] text-[10px] text-warn/90">— {e.reason} <span className="text-dim">· not an access-control decision</span></div>
          </div>
        ) : (
          <div key={i} className="type-in border-b border-line/50 px-3 py-1">
            <span className="text-dim">{time(e.ts)}</span>{" "}
            <span className={`font-bold ${e.decision === "DENY" ? "text-deny" : "text-allow"}`}>{e.decision === "DENY" ? "DENY " : "ALLOW"}</span>{" "}
            <span className="text-sky-300">{e.source}</span> <span className="text-slate-100">{e.tool}</span>{" "}
            <span className="text-dim">{argSummary(e)}</span>
            <div className={`pl-[4.6rem] text-[10px] ${e.decision === "DENY" ? "text-deny/80" : "text-dim"}`}>
              {e.decision === "DENY" ? `— ${e.reason}` : "— allowed by policy"}
              <span className="text-dim"> · policy{e.decision === "DENY" && e.payload.policy ? ` · ${e.payload.policy}` : ""}</span>
            </div>
          </div>
        ))}
        <div ref={end} />
      </div>
    </Panel>
  );
}

function JobsPanel({ jobs }: { jobs: Job[] }) {
  return (
    <Panel className="h-[230px] lg:h-auto" title="Recent jobs" right={<span className="font-mono text-[10px] text-dim">{jobs.length}</span>}>
      <div className="font-mono text-[11px]">
        {jobs.length === 0 && <p className="p-3 text-dim">No jobs yet. PR events and /commands from GitHub show up here.</p>}
        {jobs.map((j) => (
          <div key={j.review_id} className="border-b border-line/50 px-3 py-1">
            <span className={j.ok ? (j.gate?.failed ? "text-deny" : "text-allow") : "text-warn"}>{j.ok ? (j.gate?.failed ? "GATE FAIL" : "ok") : "error"}</span>{" "}
            <span className="text-sky-300">/{j.command}</span> <span className="text-slate-100">{j.repo}#{j.pr}</span>{" "}
            <span className="text-dim">{j.verdict ?? ""} · {j.seconds.toFixed(0)}s</span>
            {j.posted?.[0] && <> · <a className="underline" href={j.posted[0]} target="_blank" rel="noreferrer">open</a></>}
            {(j.error || j.gate?.reason) && <div className="pl-2 text-[10px] text-dim">{j.error || j.gate?.reason}</div>}
          </div>
        ))}
      </div>
    </Panel>
  );
}

const SEV: Record<string, string> = {
  CRITICAL: "bg-deny text-black", HIGH: "bg-deny/25 text-deny", MEDIUM: "bg-warn/25 text-warn", LOW: "bg-slate-700 text-slate-200", INFO: "bg-slate-800 text-dim",
};

function ReviewPanel({ review, events, blocked }: { review: Review | null; events: Ev[]; blocked: number }) {
  const waiting = events.some((e) => e.type === "hitl.waiting");
  const approved = events.some((e) => e.type === "hitl.approved");
  const posted = events.filter((e) => e.type === "review.posted");
  const live = !review ? events.filter((e) => e.type === "finding").map((e) => e.payload as Review["findings"][number]) : review.findings;
  const top = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"].find((s) => live.some((f) => f.severity === s)) ?? "NONE";
  const byAgent = live.reduce<Record<string, typeof live>>((acc, f) => ({ ...acc, [f.agent_type]: [...(acc[f.agent_type] ?? []), f] }), {});

  return (
    <Panel className="h-[340px] lg:h-auto" title="Review" right={review && <span className={`font-mono text-[10px] ${review.verdict === "NEEDS_HUMAN" ? "text-warn" : "text-slate-300"}`}>{review.verdict} · conf {review.overall_confidence.toFixed(2)}</span>}>
      <div className="p-3 text-[12px]">
        {live.length === 0 && !review && <p className="font-mono text-[11px] text-dim">No review yet.</p>}
        {(review || live.length > 0) && (
          <div className={`mb-2 border px-2 py-1 font-mono text-[11px] ${top === "CRITICAL" || top === "HIGH" ? "border-deny/60 text-deny" : "border-line text-slate-200"}`}>
            Risk: {top} · {live.filter((f) => f.severity === "CRITICAL").length} critical · {blocked} blocked action{blocked === 1 ? "" : "s"}
          </div>
        )}
        {waiting && !approved && <div className="mb-2 border border-warn/60 px-2 py-1 font-mono text-[11px] text-warn">Waiting for a human. APPROVE is locked for this PR.</div>}
        {approved && <div className="mb-2 border border-allow/60 px-2 py-1 font-mono text-[11px] text-allow">Human approved. APPROVE unlocked for this PR only.</div>}
        {Object.entries(byAgent).map(([agent, list]) => (
          <div key={agent} className="mb-2">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-widest text-dim">{agent}</div>
            {list.map((f, i) => (
              <div key={i} className="type-in mb-1.5 border-l-2 border-line pl-2">
                <div>
                  <span className={`mr-1.5 px-1 py-px font-mono text-[9.5px] font-bold ${SEV[f.severity]}`}>{f.severity}</span>
                  <span className="text-slate-100">{f.summary}</span>
                </div>
                <div className="font-mono text-[10px] text-dim">
                  {f.file_path}:{f.line_start} · {f.category} · conf {f.confidence.toFixed(2)} ·{" "}
                  <span className={f.checker_status === "CONFIRMED" ? "text-allow" : f.checker_status === "DOWNGRADED" ? "text-warn" : "text-dim"}>checker: {f.checker_status}</span>
                </div>
              </div>
            ))}
          </div>
        ))}
        {review?.checker_notes && <p className="font-mono text-[10px] text-dim">Checker: {review.checker_notes}</p>}
        {review?.notes?.map((n, i) => <p key={i} className="font-mono text-[10px] text-dim">{n}</p>)}
        {posted.map((p, i) => (
          <p key={i} className="mt-1 font-mono text-[10.5px] text-allow">
            {p.payload.updated ? "Updated" : "Posted"} {p.payload.event === "APPROVE" ? "approval" : "PRGuard comment"}{p.payload.url && <> · <a className="underline" href={p.payload.url} target="_blank" rel="noreferrer">open on GitHub</a></>}
          </p>
        ))}
      </div>
    </Panel>
  );
}

export default function App() {
  const { events, connected } = useEvents();
  const [server, setServer] = useState<ServerState | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [target, setTarget] = useState("");
  const [command, setCommand] = useState("review");
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(() => {
    getState().then(setServer).catch((e) => { setServer(null); if (String(e.message) === "401") setError("this server needs an API token (key button)"); });
    getJobs().then(setJobs).catch(() => setJobs([]));
  }, []);
  useEffect(() => { refresh(); }, [refresh, connected]);
  const doneCount = events.filter((e) => e.type === "command.done").length;
  useEffect(() => { refresh(); }, [refresh, doneCount]);
  useEffect(() => {
    if (server?.upstream === "mock" && server.prs.length && !target) setTarget(String(server.prs[0].number));
  }, [server, target]);

  const run = useMemo(() => latestRun(events), [events]);
  const rev = useMemo(() => latestReview(events), [events]);
  const review = useMemo(() => {
    const ready = [...rev.events].reverse().find((e) => e.type === "review.ready");
    return ready ? (ready.payload as Review) : null;
  }, [rev]);
  const running = run.id !== "" && !run.events.some((e) => e.type === "span.end" && e.source === "pr");
  const blocked = rev.events.filter((e) => e.type === "decision" && e.decision === "DENY").length;
  const canApprove = review?.verdict === "NEEDS_HUMAN" && !rev.events.some((e) => e.type === "hitl.approved");

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label); setError("");
    try { await fn(); } catch (e) { setError(String((e as Error).message)); }
    setBusy(""); refresh();
  };
  const start = () => act("run", () => post("/run", {
    ...(/^\d+$/.test(target.trim()) ? { pr_number: Number(target.trim()) } : { pr_url: target.trim() }),
    command, question,
  }));
  const askToken = () => { const t = window.prompt("PRGuard API token (PRGUARD_API_TOKEN)", token()); if (t !== null) { setToken(t.trim()); window.location.reload(); } };

  const btn = "whitespace-nowrap border border-line px-2.5 py-1.5 font-mono text-[10.5px] uppercase tracking-wider text-slate-200 hover:border-slate-400 disabled:opacity-35 lg:px-3 lg:text-[11px]";
  const field = "border border-line bg-ink px-2 py-1.5 font-mono text-[11px] text-slate-200";

  return (
    <div className="flex min-h-full flex-col lg:h-full">
      <header className="flex flex-wrap items-center gap-2 border-b border-line bg-panel px-3 py-2 lg:gap-3 lg:px-4">
        <div className="mr-2 font-mono text-[15px] font-bold tracking-[0.2em] text-slate-100">PR<span className="text-allow">GUARD</span></div>
        {server?.upstream === "mock" ? (
          <select value={target} onChange={(e) => setTarget(e.target.value)} className={`max-w-[230px] truncate ${field}`}>
            {server.prs.map((p) => <option key={p.number} value={p.number}>PR #{p.number} · {p.title}</option>)}
          </select>
        ) : (
          <input value={target} onChange={(e) => setTarget(e.target.value)} placeholder="https://github.com/org/repo/pull/123" className={`w-[300px] max-w-full ${field}`} />
        )}
        <select value={command} onChange={(e) => setCommand(e.target.value)} className={field}>
          {(server?.commands ?? ["review", "describe", "improve", "ask"]).map((c) => <option key={c} value={c}>/{c}</option>)}
        </select>
        {command === "ask" && <input value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="your question" className={`w-[220px] ${field}`} />}
        <button className={btn} disabled={!!busy || running || !target || (command === "ask" && !question)} onClick={start}>{running ? "Running…" : "Run"}</button>
        <div className="hidden flex-1 lg:block" />
        <button className={`${btn} ${canApprove ? "border-warn text-warn" : ""}`} disabled={!canApprove || !!busy} onClick={() => act("approve", () => post(`/approve/${rev.id}`))}>Human approve</button>
        <button className={btn} disabled={!!busy || running} onClick={() => act("reset", () => post("/reset"))}>Clear</button>
        <button className={btn} onClick={askToken} title="API token">🔑</button>
        <span className={`h-2 w-2 rounded-full ${connected ? "bg-allow" : "bg-deny"}`} title={connected ? "connected" : "disconnected"} />
      </header>

      {(error || !connected || server) && (
        <div className="flex flex-wrap gap-x-4 gap-y-0.5 border-b border-line px-3 py-1 font-mono text-[10px] text-dim lg:px-4">
          {!connected && <span className="text-deny">backend not reachable, retrying…</span>}
          {error && <span className="text-deny">{error}</span>}
          {server && <>
            <span className={server.upstream === "mock" ? "text-warn" : ""}>github: {server.upstream === "mock" ? "MOCK fixtures" : "REST API"}</span>
            <span>repos: {server.allowed_repos}</span>
            <span className={server.llm === "MOCK LLM" ? "text-warn" : ""}>reviewers: {server.llm}{server.llm_url ? ` @ ${server.llm_url}` : ""}</span>
            <span>checker: {server.checker}</span>
            <span className={server.sandbox === "ready" ? "" : "text-warn"}>sandbox: {server.sandbox}</span>
          </>}
        </div>
      )}

      <main className="grid flex-1 grid-cols-1 gap-px bg-line lg:min-h-0 lg:grid-cols-[minmax(0,1fr)_430px] lg:grid-rows-[minmax(0,1fr)_260px]">
        <section className="relative h-[330px] min-h-0 bg-ink lg:h-auto">
          <div className="absolute inset-0"><FlowGraph events={run.events} /></div>
        </section>
        <DecisionFeed events={events} />
        <div className="grid grid-cols-1 gap-px bg-line sm:grid-cols-2 lg:min-h-0">
          <JobsPanel jobs={jobs} />
          <ReviewPanel review={review} events={rev.events} blocked={blocked} />
        </div>
        <Panel title="Sandbox profile" className="h-[230px] lg:h-auto">
          <pre className="whitespace-pre-wrap p-3 font-mono text-[10.5px] text-dim">
            {(() => {
              const s = [...rev.events].reverse().find((e) => e.type === "sandbox");
              if (!s) return "docker run --network none --read-only --cap-drop ALL\n  --user nobody  (no -e, no --env-file)\n\nIn CI, PRGuard can read your pipeline's own\ntest output instead (--checks-file).";
              const { status, ...rest } = s.payload;
              return `${status}\n` + Object.entries(rest).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : v}`).join("\n");
            })()}
          </pre>
        </Panel>
      </main>
    </div>
  );
}
