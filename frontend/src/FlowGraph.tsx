import { Handle, Position, ReactFlow, useNodesState, type Edge, type Node, type NodeProps, type ReactFlowInstance } from "@xyflow/react";
import { motion } from "framer-motion";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Ev } from "./store";

const ALLOW = "#22c55e";
const DENY = "#ef4444";
const IDLE = "#253041";
const FLASH_MS = 1100;

/** Event source -> node id. */
const SOURCE_NODE: Record<string, string> = {
  pr: "pr",
  "prguard-orchestrator": "orchestrator",
  sandbox: "sandbox",
  "prguard-security": "security",
  "prguard-quality": "quality",
  "prguard-tests": "tests",
  "prguard-docs": "docs",
  "prguard-ci-fixer": "ci",
  "prguard-aggregator": "aggregator",
  "prguard-checker": "checker",
  "prguard-config": "orchestrator",
  "prguard-describer": "aggregator",
  "prguard-improver": "aggregator",
  "prguard-asker": "aggregator",
  "hitl-gate": "hitl",
};
const SPECIALISTS = ["security", "quality", "tests", "docs", "ci"];
const GATED = ["orchestrator", ...SPECIALISTS, "aggregator"];

type Data = { label: string; sub?: string; live?: boolean; tone?: "allow" | "deny" | "warn" | "gate"; shake?: number };

function Box({ data }: NodeProps<Node<Data>>) {
  const tone =
    data.tone === "deny" ? "border-deny text-deny" :
    data.tone === "allow" ? "border-allow text-allow" :
    data.tone === "warn" ? "border-warn text-warn" :
    data.tone === "gate" ? "border-sky-500/70 text-sky-300" : "border-line text-slate-200";
  return (
    <motion.div
      key={data.shake ?? 0}
      animate={data.shake ? { x: [0, -7, 7, -5, 5, 0] } : undefined}
      transition={{ duration: 0.4 }}
      className={`rounded-md border bg-panel px-3 py-2 text-center font-mono text-[11px] leading-tight ${tone} ${data.live ? "node-live" : ""}`}
      style={{ width: 132 }}
    >
      <Handle type="target" position={Position.Left} />
      <Handle type="target" position={Position.Top} id="top" />
      <div className="font-semibold tracking-wide">{data.label}</div>
      {data.sub && <div className="mt-0.5 text-[9.5px] text-dim">{data.sub}</div>}
      <Handle type="source" position={Position.Right} />
      <Handle type="source" position={Position.Bottom} id="bottom" />
    </motion.div>
  );
}

const nodeTypes = { box: Box };

const POS: Record<string, [number, number]> = {
  pr: [0, 170],
  orchestrator: [170, 170],
  sandbox: [170, 330],
  security: [350, 0],
  quality: [350, 85],
  tests: [350, 170],
  docs: [350, 255],
  ci: [350, 340],
  aggregator: [530, 170],
  checker: [700, 170],
  hitl: [870, 170],
  gate: [530, 460],
  github: [870, 460],
};

export default function FlowGraph({ events }: { events: Ev[] }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 200);
    return () => window.clearInterval(t);
  }, []);

  const { live, flashes, lastDeny, sandboxSub } = useMemo(() => {
    const open = new Map<string, number>();
    const flashes = new Map<string, { decision: "ALLOW" | "DENY"; at: number }>();
    let lastDeny = 0;
    let sandboxSub = "";
    for (const e of events) {
      const node = SOURCE_NODE[e.source];
      if (node && e.type === "span.start") open.set(node, (open.get(node) ?? 0) + 1);
      if (node && e.type === "span.end") open.set(node, Math.max(0, (open.get(node) ?? 0) - 1));
      if (e.type === "decision" && node && e.decision) {
        flashes.set(node, { decision: e.decision, at: e.ts * 1000 });
        if (e.decision === "DENY") lastDeny = e.ts * 1000;
      }
      if (e.type === "sandbox") {
        if (e.payload.status === "started") open.set("sandbox", 1);
        else open.set("sandbox", 0);
        sandboxSub = e.payload.status === "unavailable" ? "PR code not run" : "docker · no network";
      }
    }
    return { live: open, flashes, lastDeny, sandboxSub };
  }, [events]);

  const recent = (at: number) => now - at < FLASH_MS;

  const hitlWaiting = events.some((e) => e.type === "hitl.waiting") && !events.some((e) => e.type === "hitl.approved");
  const data: Record<string, Data> = {
    pr: { label: "PR" },
    orchestrator: { label: "ORCHESTRATOR", sub: "build context" },
    sandbox: { label: "SANDBOX", sub: sandboxSub || "docker · no network" },
    security: { label: "SECURITY" },
    quality: { label: "QUALITY" },
    tests: { label: "TESTS" },
    docs: { label: "DOCS" },
    ci: { label: "CI FIXER", sub: "logs · re-run only" },
    aggregator: { label: "PUBLISH", sub: "review · describe · improve" },
    checker: { label: "CHECKER", sub: "maker ≠ checker" },
    hitl: { label: "HITL GATE", tone: hitlWaiting ? "warn" : undefined },
    gate: {
      label: "POLICY GATE",
      sub: "default deny · per call",
      tone: "gate",
      shake: recent(lastDeny) ? lastDeny : 0,
    },
    github: { label: "GITHUB", sub: "REST · token held here" },
  };
  for (const id of Object.keys(data)) data[id].live = (live.get(id) ?? 0) > 0;

  // React Flow owns the node objects (and their measured sizes); we only swap `data`.
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<Data>>(
    Object.entries(POS).map(([id, [x, y]]) => ({ id, type: "box", position: { x, y }, data: data[id], draggable: false })),
  );
  const signature = JSON.stringify(data);
  useEffect(() => {
    setNodes((current) => current.map((n) => ({ ...n, data: data[n.id] })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, setNodes]);

  const flow = (id: string, source: string, target: string, extra: Partial<Edge> = {}): Edge => ({
    id, source, target, style: { stroke: IDLE, strokeWidth: 1.2 }, ...extra,
  });
  const edges: Edge[] = [
    flow("pr-orch", "pr", "orchestrator"),
    flow("orch-sandbox", "orchestrator", "sandbox", { sourceHandle: "bottom", targetHandle: "top" }),
    ...SPECIALISTS.flatMap((r) => [
      flow(`orch-${r}`, "orchestrator", r),
      flow(`${r}-agg`, r, "aggregator"),
    ]),
    flow("agg-checker", "aggregator", "checker"),
    flow("checker-hitl", "checker", "hitl"),
  ];

  let anyAllow = false;
  for (const agent of GATED) {
    const f = flashes.get(agent);
    const on = f && recent(f.at);
    if (on && f.decision === "ALLOW") anyAllow = true;
    edges.push({
      id: `${agent}-gate`, source: agent, target: "gate", sourceHandle: "bottom", targetHandle: "top",
      animated: !!on,
      style: { stroke: on ? (f.decision === "ALLOW" ? ALLOW : DENY) : IDLE, strokeWidth: on ? 2.6 : 1, strokeDasharray: on ? undefined : "3 4" },
    });
  }
  // A denied call never reaches GitHub: only ALLOW lights the last hop.
  edges.push({
    id: "gate-github", source: "gate", target: "github", animated: anyAllow,
    style: { stroke: anyAllow ? ALLOW : IDLE, strokeWidth: anyAllow ? 2.6 : 1.2 },
  });

  // Keep the whole graph in view whenever the panel changes size.
  const wrapper = useRef<HTMLDivElement>(null);
  const instance = useRef<ReactFlowInstance<Node<Data>, Edge> | null>(null);
  useEffect(() => {
    if (!wrapper.current) return;
    const observer = new ResizeObserver(() => instance.current?.fitView({ padding: 0.08 }));
    observer.observe(wrapper.current);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={wrapper} className="h-full w-full">
    <ReactFlow
      onInit={(rf) => { instance.current = rf; window.setTimeout(() => rf.fitView({ padding: 0.08 }), 60); }}
      nodes={nodes} edges={edges} onNodesChange={onNodesChange} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.08 }} minZoom={0.2}
      nodesConnectable={false} elementsSelectable={false} panOnDrag={false} zoomOnScroll={false}
      zoomOnDoubleClick={false} zoomOnPinch={false} preventScrolling={false}
    />
    </div>
  );
}
