import { useEffect, useReducer, useState } from "react";

// Served by the PRGuard server: same origin. Vite dev server (:5173): the API on :8000.
export const API = (import.meta.env.VITE_API as string | undefined)
  ?? (window.location.port === "5173" ? "http://127.0.0.1:8000" : window.location.origin);

/** Optional PRGUARD_API_TOKEN, remembered in this browser. */
export function token(): string {
  try { return localStorage.getItem("prguard_token") ?? ""; } catch { return ""; }
}
export function setToken(value: string) {
  try { localStorage.setItem("prguard_token", value); } catch { /* storage blocked */ }
}
function authHeaders(): Record<string, string> {
  const t = token();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export interface Ev {
  ts: number;
  review_id: string;
  source: string;
  type: string;
  tool?: string | null;
  args_redacted?: Record<string, unknown> | null;
  decision?: "ALLOW" | "DENY" | null;
  reason?: string | null;
  enforcer?: string | null;
  payload: Record<string, any>;
}

export interface Finding {
  agent_type: string;
  severity: string;
  category: string;
  summary: string;
  file_path: string;
  line_start: number;
  suggestion: string;
  confidence: number;
  rationale: string;
  checker_status: string;
  agreed_by: string[];
}

export interface Review {
  review_id: string;
  findings: Finding[];
  overall_confidence: number;
  verdict: "COMMENT" | "REQUEST_CHANGES" | "NEEDS_HUMAN";
  checker_notes: string;
  posted_url: string;
  notes?: string[];
}

export interface ServerState {
  enforcer: string;
  upstream: string;
  llm: string;
  llm_url: string;
  checker: string;
  target_repo: string;
  allowed_repos: string;
  prs: { number: number; title: string }[];
  sandbox: string;
  commands: string[];
}

export interface Job {
  review_id: string;
  command: string;
  repo: string;
  pr: number;
  ok: boolean;
  error: string;
  gate: { failed?: boolean; reason?: string };
  posted: string[];
  seconds: number;
  verdict: string | null;
}

interface Store {
  events: Ev[];
}

type Action = { kind: "event"; ev: Ev } | { kind: "clear" };

function reducer(state: Store, action: Action): Store {
  if (action.kind === "clear") return { events: [] };
  return { events: [...state.events.slice(-1500), action.ev] };
}

/** Subscribe to the backend event stream, reconnecting automatically. */
export function useEvents() {
  const [store, dispatch] = useReducer(reducer, { events: [] });
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    // Per-effect flag: a shared ref would let a stale socket reconnect after remount.
    const closed = { current: false };
    let socket: WebSocket | null = null;
    let timer: number | undefined;

    const connect = () => {
      const t = token();
      const ws = new WebSocket(API.replace(/^http/, "ws") + "/ws" + (t ? `?token=${encodeURIComponent(t)}` : ""));
      socket = ws;
      ws.onopen = () => {
        if (closed.current) return ws.close();
        dispatch({ kind: "clear" }); // the server replays its history on connect
        setConnected(true);
      };
      ws.onmessage = (m) => {
        if (!closed.current) dispatch({ kind: "event", ev: JSON.parse(m.data) });
      };
      ws.onclose = () => {
        if (closed.current) return;
        setConnected(false);
        timer = window.setTimeout(connect, 1200);
      };
      ws.onerror = () => ws.close();
    };
    connect();
    return () => {
      closed.current = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return { events: store.events, connected, clear: () => dispatch({ kind: "clear" }) };
}

export async function post(path: string, body?: unknown) {
  const res = await fetch(API + path, {
    method: "POST",
    headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ?? `${res.status}`);
  return data;
}

export async function get<T>(path: string): Promise<T> {
  const res = await fetch(API + path, { headers: authHeaders() });
  if (res.status === 401) throw new Error("401");
  return res.json();
}

export const getState = () => get<ServerState>("/state");
export const getJobs = () => get<Job[]>("/jobs");

/** Events belonging to the most recent command of any kind. */
export function latestRun(events: Ev[]): { id: string; events: Ev[] } {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "span.start" && e.source === "pr") {
      return { id: e.review_id, events: events.filter((x) => x.review_id === e.review_id) };
    }
  }
  return { id: "", events: [] };
}

/** The most recent /review. */
export function latestReview(events: Ev[]): { id: string; events: Ev[] } {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "span.start" && e.source === "pr" && (e.payload.command ?? "review") === "review") {
      return { id: e.review_id, events: events.filter((x) => x.review_id === e.review_id) };
    }
  }
  return { id: "", events: [] };
}
