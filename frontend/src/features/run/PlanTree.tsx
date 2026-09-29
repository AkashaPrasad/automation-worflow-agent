import { memo, useCallback, useEffect, useMemo, useState } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import dagre from "@dagrejs/dagre";
import { BadgeCheck, CircleDashed, CircleX, ListTree, Maximize2, Minimize2, Target } from "lucide-react";
import type { Plan, PlanNode } from "../../lib/types";
import { NODE_STATUS, TONE, VERDICT, appMeta } from "../../lib/semantics";
import { pct } from "../../lib/format";
import { useTheme } from "../../lib/theme";
import { Dot, ReplannedBadge, TaintBadge } from "../../components/ui/badges";
import { EmptyState, cx } from "../../components/ui/primitives";
import { Tooltip } from "../../components/ui/Tooltip";

const ACTION_W = 236;
const ACTION_H = 104;
const GOAL_W = 244;
const GOAL_H = 86;
const GROUP_PAD = 14;

type PlanNodeData = { node: PlanNode; selected: boolean; onOpen: (id: string) => void; children?: PlanNode[] };
type GroupData = { title: string; status: PlanNode["status"] };

// ---------------------------------------------------------------------------
// layout
// ---------------------------------------------------------------------------

function descendants(plan: Plan, id: string, out: string[] = []): string[] {
  for (const c of plan.nodes[id]?.children ?? []) {
    if (!plan.nodes[c]) continue;
    out.push(c);
    descendants(plan, c, out);
  }
  return out;
}

function buildGraph(plan: Plan, selectedId: string | null, onOpen: (id: string) => void) {
  const nodes = Object.values(plan.nodes);
  const byId = plan.nodes;
  const edgesOut: Edge[] = [];
  const edgeKeys = new Set<string>();
  const layoutEdges: [string, string, number][] = [];
  const addEdge = (source: string, target: string, kind: "tree" | "dep" | "cross") => {
    const k = `${source}->${target}`;
    if (edgeKeys.has(k) || !byId[source] || !byId[target]) return;
    edgeKeys.add(k);
    const live = byId[target].status === "running";
    // cross-goal dependencies are drawn but do not drive the layout, so each goal's subtree stays compact
    if (kind !== "cross") layoutEdges.push([source, target, kind === "tree" ? 2 : 1]);
    edgesOut.push({
      id: k,
      source,
      target,
      type: kind === "tree" ? "smoothstep" : "default",
      className: cx(kind === "tree" ? "tree-edge" : "dep-edge", kind === "cross" && "cross-edge", live && "is-live"),
      markerEnd:
        kind !== "tree" ? { type: MarkerType.ArrowClosed, width: 14, height: 14, color: live ? "var(--run-solid)" : kind === "cross" ? "var(--line-strong)" : "var(--ink-4)" } : undefined,
      style:
        kind === "tree"
          ? { stroke: "var(--line-strong)", strokeWidth: 1.25 }
          : kind === "cross"
            ? { stroke: "var(--line-strong)", strokeWidth: 1, opacity: 0.8 }
            : { stroke: "var(--ink-4)", strokeWidth: 1.25 },
      pathOptions: kind === "tree" ? { borderRadius: 10 } : undefined,
      zIndex: 1,
    } as Edge);
  };

  for (const n of nodes) {
    const siblings = new Set(n.children);
    for (const c of n.children) {
      const child = byId[c];
      if (!child) continue;
      if (child.kind === "goal") addEdge(n.id, c, "tree");
      else if (!child.depends_on.some((d) => siblings.has(d))) addEdge(n.id, c, "tree");
    }
  }
  for (const n of nodes) {
    if (n.kind !== "action") continue;
    for (const d of n.depends_on) {
      const dep = byId[d];
      if (dep?.kind !== "action") continue;
      addEdge(d, n.id, dep.parent_id === n.parent_id ? "dep" : "cross");
    }
  }

  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 18, ranksep: 42, marginx: 16, marginy: 16, ranker: "network-simplex" });
  g.setDefaultEdgeLabel(() => ({}));
  // dagre's ordering heuristic mirrors insertion order; insert in reverse so siblings read left to right as planned
  for (const n of [...nodes].reverse()) g.setNode(n.id, { width: n.kind === "goal" ? GOAL_W : ACTION_W, height: n.kind === "goal" ? GOAL_H : ACTION_H });
  for (const [a, b, weight] of [...layoutEdges].reverse()) g.setEdge(a, b, { weight });
  dagre.layout(g);

  const flowNodes: Node[] = [];
  for (const n of nodes) {
    const p = g.node(n.id);
    if (!p) continue;
    const w = n.kind === "goal" ? GOAL_W : ACTION_W;
    const h = n.kind === "goal" ? GOAL_H : ACTION_H;
    flowNodes.push({
      id: n.id,
      type: n.kind === "goal" ? "goal" : "action",
      position: { x: p.x - w / 2, y: p.y - h / 2 },
      data: { node: n, selected: n.id === selectedId, onOpen, children: n.children.map((c) => byId[c]).filter(Boolean) } satisfies PlanNodeData,
      width: w,
      height: h,
      draggable: false,
      connectable: false,
      selectable: false,
      zIndex: 2,
    });
  }

  // Group frames for sub-goals: goal + its whole subtree.
  const pos = new Map(flowNodes.map((f) => [f.id, f]));
  const groups: Node[] = [];
  for (const n of nodes) {
    if (n.kind !== "goal" || n.id === plan.root_id) continue;
    const ids = [n.id, ...descendants(plan, n.id)];
    let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    for (const id of ids) {
      const f = pos.get(id);
      if (!f) continue;
      x1 = Math.min(x1, f.position.x);
      y1 = Math.min(y1, f.position.y);
      x2 = Math.max(x2, f.position.x + (f.width ?? ACTION_W));
      y2 = Math.max(y2, f.position.y + (f.height ?? ACTION_H));
    }
    if (!Number.isFinite(x1)) continue;
    groups.push({
      id: `group-${n.id}`,
      type: "group-frame",
      position: { x: x1 - GROUP_PAD, y: y1 - GROUP_PAD },
      data: { title: n.title, status: n.status } satisfies GroupData,
      width: x2 - x1 + GROUP_PAD * 2,
      height: y2 - y1 + GROUP_PAD * 2,
      draggable: false,
      selectable: false,
      connectable: false,
      focusable: false,
      zIndex: 0,
      style: { pointerEvents: "none" },
    });
  }
  return { nodes: [...groups, ...flowNodes], edges: edgesOut };
}

// ---------------------------------------------------------------------------
// nodes
// ---------------------------------------------------------------------------

function nodeLabel(n: PlanNode): string {
  const s = NODE_STATUS[n.status]?.label ?? n.status;
  const v = n.gate ? `, gate ${VERDICT[n.gate.verdict]?.label} at ${pct(n.gate.risk)} risk` : "";
  const t = n.tainted ? ", tainted by untrusted input" : "";
  return `${n.kind === "goal" ? "Goal" : "Action"} ${n.id}: ${n.title}. ${s}${v}${t}. Press Enter for details.`;
}

function frameClasses(n: PlanNode, selected: boolean) {
  const base = "group relative h-full w-full cursor-pointer rounded-[10px] border bg-panel text-left shadow-card outline-none transition-[border-color,box-shadow,background-color] duration-300";
  const byStatus: Record<string, string> = {
    running: "border-run-line",
    simulated: "border-dashed border-shadow-line",
    awaiting_approval: "border-ask-line bg-[color-mix(in_oklch,var(--ask-bg)_45%,var(--panel))]",
    blocked: "border-block-line bg-[color-mix(in_oklch,var(--block-bg)_55%,var(--panel))]",
    failed: "border-block-line",
    compensated: "border-undo-line",
    skipped: "border-line opacity-70",
    cancelled: "border-line opacity-70",
    pending: "border-line",
    ready: "border-line",
    succeeded: "border-line",
  };
  return cx(base, byStatus[n.status] ?? "border-line", selected && "ring-2 ring-accent ring-offset-2 ring-offset-bg", "focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg");
}

function openOnKey(e: React.KeyboardEvent, open: () => void) {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    open();
  }
}

const ActionNode = memo(function ActionNode({ data }: NodeProps<Node<PlanNodeData>>) {
  const n = data.node;
  const s = NODE_STATUS[n.status] ?? NODE_STATUS.pending;
  const app = appMeta(n.tool);
  const AppI = app.icon;
  const open = () => data.onOpen(n.id);
  const verdict = n.gate ? VERDICT[n.gate.verdict] : null;
  return (
    <div
      data-testid="plan-node"
      data-node-id={n.id}
      data-status={n.status}
      data-verdict={n.gate?.verdict ?? "none"}
      data-kind="action"
      role="button"
      tabIndex={0}
      aria-label={nodeLabel(n)}
      onKeyDown={(e) => openOnKey(e, open)}
      className={frameClasses(n, data.selected)}
    >
      <Handle type="target" position={Position.Top} />
      {n.status === "running" && (
        <span className="absolute inset-x-0 top-0 h-[2px] overflow-hidden rounded-t-[10px]" aria-hidden>
          <span className="live-sweep absolute inset-y-0 w-2/5 bg-linear-to-r from-transparent via-run-solid to-transparent" />
        </span>
      )}
      <div className="flex h-full flex-col px-3 py-2.5">
        <div className="flex items-center gap-1.5">
          <AppI className="size-3.5 shrink-0 text-ink-3" aria-hidden />
          <span className="truncate font-mono text-[10.5px] text-ink-3">{n.tool}</span>
          <span className="ml-auto flex items-center gap-1">
            {verdict && n.gate && (
              <span className={cx("inline-flex h-[18px] items-center gap-1 rounded-[5px] border px-1.5 text-[10px] font-semibold tracking-[0.03em]", TONE[verdict.tone].bg, TONE[verdict.tone].fg, TONE[verdict.tone].line)}>
                {verdict.label}
                <span className="tnum font-mono font-medium opacity-80">{pct(n.gate.risk)}</span>
              </span>
            )}
          </span>
        </div>
        <p className="mt-1.5 line-clamp-2 shrink-0 text-[12.5px] font-medium leading-[1.3] text-ink">{n.title}</p>
        <div className="mt-auto flex items-center gap-1.5 pt-1.5">
          <span className={cx("inline-flex items-center gap-1 text-[10.5px] font-medium", TONE[s.tone].fg)}>
            <Dot tone={s.tone} live={s.live} />
            {s.label}
          </span>
          {n.attempts > 1 && <span className="font-mono text-[10px] text-ink-3" title={`${n.attempts} attempts`}>×{n.attempts}</span>}
          <span className="ml-auto flex items-center gap-1">
            {n.revision > 1 && <ReplannedBadge revision={n.revision} />}
            {n.tainted && <TaintBadge size="xs" compact />}
            {n.verification && (n.verification.passed ? <BadgeCheck className="size-3.5 text-auto-fg" aria-label="verified" /> : <CircleX className="size-3.5 text-block-fg" aria-label="verification failed" />)}
          </span>
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
});

const GoalNode = memo(function GoalNode({ data }: NodeProps<Node<PlanNodeData>>) {
  const n = data.node;
  const s = NODE_STATUS[n.status] ?? NODE_STATUS.pending;
  const open = () => data.onOpen(n.id);
  const v = n.verification;
  const checks = v?.checks ?? [];
  const passed = checks.filter((c) => c.passed).length;
  const kids = data.children ?? [];
  const done = kids.filter((k) => k.status === "succeeded").length;
  return (
    <div
      data-testid="plan-node"
      data-node-id={n.id}
      data-status={n.status}
      data-verdict={n.gate?.verdict ?? "none"}
      data-kind="goal"
      role="button"
      tabIndex={0}
      aria-label={nodeLabel(n)}
      onKeyDown={(e) => openOnKey(e, open)}
      className={cx(frameClasses(n, data.selected), "bg-panel-2")}
    >
      <Handle type="target" position={Position.Top} />
      <div className="flex h-full flex-col justify-center px-3 py-2">
        <div className="flex items-center gap-1.5">
          <Target className="size-3.5 shrink-0 text-ink-3" aria-hidden />
          <span className="font-mono text-[10.5px] text-ink-3">{n.id}</span>
          <span className={cx("inline-flex items-center gap-1 text-[10.5px] font-medium", TONE[s.tone].fg)}>
            <Dot tone={s.tone} live={s.live} />
            {s.label}
          </span>
          <span className="ml-auto flex items-center gap-1 text-[10.5px] text-ink-3">
            {n.revision > 1 && <ReplannedBadge revision={n.revision} />}
            {v ? (
              <Tooltip content={`Proof of done: ${passed} of ${checks.length} success criteria met (Jev).`}>
                <span tabIndex={-1} className={cx("inline-flex items-center gap-1 font-medium", v.passed ? "text-auto-fg" : "text-block-fg")}>
                  {v.passed ? <BadgeCheck className="size-3.5" aria-hidden /> : <CircleX className="size-3.5" aria-hidden />}
                  {passed}/{checks.length}
                </span>
              </Tooltip>
            ) : n.success_criteria.length ? (
              <span className="inline-flex items-center gap-1" title="Success criteria not verified yet">
                <CircleDashed className="size-3" aria-hidden />
                {n.success_criteria.length}
              </span>
            ) : null}
          </span>
        </div>
        <p className="mt-1 line-clamp-2 shrink-0 text-[13px] font-semibold leading-[1.3] text-ink">{n.title}</p>
        {kids.length > 0 && (
          <div className="mt-1.5 flex h-1 gap-0.5" aria-hidden>
            {kids.map((k) => (
              <span key={k.id} className={cx("h-full flex-1 rounded-full transition-colors duration-300", TONE[NODE_STATUS[k.status]?.tone ?? "neutral"].solid, k.status === "pending" && "opacity-40")} />
            ))}
          </div>
        )}
        <span className="sr-only">{`${done} of ${kids.length} children succeeded`}</span>
      </div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
});

function GroupFrame({ data }: NodeProps<Node<GroupData>>) {
  return (
    <div className="pointer-events-none h-full w-full rounded-[14px] border border-dashed border-line-strong/80 bg-[color-mix(in_oklch,var(--panel-2)_55%,transparent)]" aria-hidden>
      <span className="sr-only">{data.title}</span>
    </div>
  );
}

const nodeTypes = { action: ActionNode, goal: GoalNode, "group-frame": GroupFrame };

// ---------------------------------------------------------------------------

function Flow({ plan, selectedId, onOpen, expanded }: { plan: Plan; selectedId: string | null; onOpen: (id: string) => void; expanded: boolean }) {
  const { resolved } = useTheme();
  const { fitView } = useReactFlow();
  const graph = useMemo(() => buildGraph(plan, selectedId, onOpen), [plan, selectedId, onOpen]);
  const structureKey = useMemo(() => `${plan.revision}:${Object.keys(plan.nodes).sort().join(",")}`, [plan]);

  useEffect(() => {
    const t = setTimeout(() => void fitView({ padding: 0.06, duration: 300, maxZoom: 1.05 }), 60);
    return () => clearTimeout(t);
  }, [structureKey, expanded, fitView]);

  return (
    <ReactFlow
      nodes={graph.nodes}
      edges={graph.edges}
      nodeTypes={nodeTypes}
      onNodeClick={(_, n) => {
        if (n.type !== "group-frame") onOpen(n.id);
      }}
      colorMode={resolved}
      fitView
      fitViewOptions={{ padding: 0.06, maxZoom: 1.05 }}
      minZoom={0.15}
      maxZoom={1.6}
      nodesDraggable={false}
      nodesConnectable={false}
      nodesFocusable={false}
      edgesFocusable={false}
      elementsSelectable={false}
      zoomOnScroll={false}
      panOnScroll={false}
      preventScrolling={expanded}
      zoomOnPinch
      attributionPosition="top-right"
      zoomOnDoubleClick
      panOnDrag
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--line-strong)" />
      <Controls showInteractive={false} position="bottom-left" aria-label="Zoom controls" />
      <MiniMap
        pannable
        zoomable
        position="bottom-right"
        className="!hidden md:!block"
        style={{ width: 150, height: 96 }}
        nodeColor={(n) => {
          if (n.type === "group-frame") return "transparent";
          const pn = (n.data as PlanNodeData).node;
          const tone = pn.gate?.verdict === "block" ? "block" : NODE_STATUS[pn.status]?.tone ?? "neutral";
          return `var(--${tone}-solid)`;
        }}
        nodeStrokeWidth={0}
        maskColor="color-mix(in oklch, var(--bg) 65%, transparent)"
      />
    </ReactFlow>
  );
}

export function PlanTree({ plan, selectedId, onOpen, planning, waiting }: { plan: Plan | null | undefined; selectedId: string | null; onOpen: (id: string) => void; planning?: boolean; waiting?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const toggle = useCallback(() => setExpanded((x) => !x), []);

  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setExpanded(false);
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [expanded]);

  const hasPlan = !!plan && Object.keys(plan.nodes).length > 0;
  const counts = useMemo(() => {
    const ns = Object.values(plan?.nodes ?? {});
    const actions = ns.filter((n) => n.kind === "action");
    return {
      goals: ns.length - actions.length,
      actions: actions.length,
      done: actions.filter((n) => n.status === "succeeded").length,
    };
  }, [plan]);

  return (
    <section
      className={cx(
        "flex flex-col overflow-hidden border border-line bg-panel shadow-card",
        expanded ? "fixed inset-0 z-[60] rounded-none" : "relative rounded-xl",
      )}
      aria-labelledby="plan-title"
    >
      <header className="flex min-h-11 flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-4 py-2">
        <h2 id="plan-title" className="flex items-center gap-2 text-sm font-semibold text-ink">
          <ListTree className="size-4 text-ink-3" aria-hidden />
          Plan tree
        </h2>
        {plan && (
          <p className="text-xs text-ink-3">
            {counts.goals} goals · {counts.actions} actions · {counts.done} done · rev {plan.revision}
          </p>
        )}
        <div className="ml-auto flex items-center gap-2">
          <Legend />
          <button
            type="button"
            onClick={toggle}
            className="grid size-7 place-items-center rounded-md text-ink-3 hover:bg-panel-3 hover:text-ink"
            aria-label={expanded ? "Exit full screen" : "Expand plan tree to full screen"}
            aria-pressed={expanded}
          >
            {expanded ? <Minimize2 className="size-3.5" aria-hidden /> : <Maximize2 className="size-3.5" aria-hidden />}
          </button>
        </div>
      </header>
      <div data-testid="plan-tree" className={cx("relative grid-dots", expanded ? "flex-1" : hasPlan ? "h-[460px] sm:h-[560px] xl:h-[620px]" : "h-[260px]")}>
        {hasPlan ? (
          <ReactFlowProvider>
            <Flow plan={plan} selectedId={selectedId} onOpen={onOpen} expanded={expanded} />
          </ReactFlowProvider>
        ) : (
          <EmptyState icon={ListTree} title={waiting ? "Waiting for your answer" : planning ? "Muse is building the plan" : "No plan"} className="h-full">
            {waiting
              ? "Muse plans as soon as you answer the question above."
              : planning
                ? "The goal tree appears here as soon as it passes validation: tools exist, arguments match their schemas, templates point at earlier steps."
                : "This run ended before a plan was made."}
          </EmptyState>
        )}
      </div>
    </section>
  );
}

function Legend() {
  const items: { tone: keyof typeof TONE; label: string; dashed?: boolean }[] = [
    { tone: "run", label: "running" },
    { tone: "shadow", label: "simulated", dashed: true },
    { tone: "ask", label: "needs you" },
    { tone: "auto", label: "done" },
    { tone: "block", label: "blocked" },
  ];
  return (
    <ul className="hidden items-center gap-2.5 lg:flex" aria-label="Legend">
      {items.map((i) => (
        <li key={i.label} className="flex items-center gap-1 text-2xs text-ink-3">
          <span className={cx("size-2 rounded-[3px] border", TONE[i.tone].line, TONE[i.tone].bg, i.dashed && "border-dashed")} aria-hidden />
          {i.label}
        </li>
      ))}
      <li className="flex items-center gap-1 text-2xs text-ink-3">
        <span className="taint-stripes size-2 rounded-[3px] border border-taint-line" aria-hidden />
        untrusted
      </li>
    </ul>
  );
}
