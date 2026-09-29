import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { subscribeRun, type StreamState } from "./sse";
import { TERMINAL_STATUSES, type EffectRecord, type PlanNode, type RecoveryDecision, type RunDetail, type RunEvent, type RunStatus } from "./types";

/** Apply one event to the cached snapshot. Every branch is idempotent (safe to re-apply). */
export function applyEvent(d: RunDetail, ev: RunEvent): RunDetail {
  const data = ev.data ?? {};
  const run = { ...d.run };
  let approval = d.approval;
  let effects = d.effects;

  const patchNode = (fn: (n: PlanNode) => PlanNode) => {
    if (!run.plan || !ev.node_id || !run.plan.nodes[ev.node_id]) return;
    run.plan = { ...run.plan, nodes: { ...run.plan.nodes, [ev.node_id]: fn({ ...run.plan.nodes[ev.node_id] }) } };
  };
  const upsertEffect = (e: EffectRecord | undefined) => {
    if (!e?.id) return;
    const i = effects.findIndex((x) => x.id === e.id);
    effects = i >= 0 ? effects.map((x, j) => (j === i ? { ...x, ...e } : x)) : [...effects, e];
  };

  switch (ev.type) {
    case "run.status":
      if (data.status) run.status = data.status as RunStatus;
      break;
    case "intent.parsed":
      if (data.intent) run.intent = data.intent as RunDetail["run"]["intent"];
      break;
    case "clarification.requested":
      run.clarification = { question: String(data.question ?? ""), answer: null };
      break;
    case "clarification.answered":
      run.clarification = { question: run.clarification?.question ?? "", answer: String(data.answer ?? "") };
      break;
    case "plan.created":
    case "plan.revised":
      if (data.plan) run.plan = data.plan as RunDetail["run"]["plan"];
      break;
    case "node.status":
      patchNode((n) => ({ ...n, status: (data.status as PlanNode["status"]) ?? n.status, attempts: typeof data.attempts === "number" ? data.attempts : n.attempts }));
      break;
    case "node.started":
      patchNode((n) => ({ ...n, status: "running", started_at: ev.ts }));
      break;
    case "node.result":
      patchNode((n) => ({ ...n, result: (data.result as PlanNode["result"]) ?? n.result, finished_at: ev.ts }));
      break;
    case "node.gated":
      patchNode((n) => ({ ...n, gate: (data.gate as PlanNode["gate"]) ?? n.gate }));
      break;
    case "node.verified":
      patchNode((n) => ({ ...n, verification: (data.verification as PlanNode["verification"]) ?? n.verification }));
      break;
    case "recovery.decided":
      patchNode((n) => {
        const dec = data.decision as RecoveryDecision | undefined;
        if (!dec) return n;
        const key = JSON.stringify(dec);
        return n.recovery.some((r) => JSON.stringify(r) === key) ? n : { ...n, recovery: [...n.recovery, dec] };
      });
      break;
    case "approval.requested":
    case "approval.resolved":
      if (data.approval) approval = data.approval as RunDetail["approval"];
      break;
    case "effect.recorded":
    case "effect.compensated":
      upsertEffect(data.effect as EffectRecord | undefined);
      break;
    case "shadow.completed":
      for (const e of (data.effects as EffectRecord[]) ?? []) upsertEffect(e);
      break;
    case "run.completed":
      if (typeof data.summary === "string") run.summary = data.summary;
      break;
    case "run.failed":
      if (typeof data.error === "string") run.error = data.error;
      break;
    default:
      break;
  }
  run.updated_at = Math.max(run.updated_at, ev.ts);
  return { run, approval, effects };
}

export interface RunState {
  detail: RunDetail | undefined;
  events: RunEvent[];
  isLoading: boolean;
  error: unknown;
  stream: StreamState;
  refetch: () => void;
  /** Poll snapshot + events briefly (e.g. after a control action on a finished run, when no stream is open). */
  boost: (ms?: number) => void;
  /** seq of the newest event, used to highlight what just changed */
  lastEventAt: number;
}

export function useRun(id: string): RunState {
  const qc = useQueryClient();
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [stream, setStream] = useState<StreamState>("connecting");
  const [lastEventAt, setLastEventAt] = useState(0);
  const lastSeq = useRef(0);
  const seen = useRef(new Set<number>());
  const refetchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const firstPending = useRef<number | null>(null);
  const [boostUntil, setBoostUntil] = useState(0);

  const query = useQuery({
    queryKey: ["run", id],
    queryFn: () => api.getRun(id),
    refetchInterval: (q) => {
      const s = q.state.data?.run.status;
      if (Date.now() < boostUntil) return 1000;
      if (!s || TERMINAL_STATUSES.has(s)) return false;
      return stream === "live" ? 15_000 : 4000; // fallback polling when the stream is down
    },
  });

  const status = query.data?.run.status;
  const terminal = status ? TERMINAL_STATUSES.has(status) : false;

  const scheduleRefetch = useCallback(() => {
    // Debounced, with a max wait so a busy stream still reconciles regularly.
    const now = Date.now();
    if (firstPending.current == null) firstPending.current = now;
    if (refetchTimer.current) clearTimeout(refetchTimer.current);
    const wait = now - firstPending.current > 2000 ? 0 : 450;
    refetchTimer.current = setTimeout(() => {
      firstPending.current = null;
      void qc.invalidateQueries({ queryKey: ["run", id] });
      void qc.invalidateQueries({ queryKey: ["runs"] });
    }, wait);
  }, [qc, id]);

  const ingest = useCallback(
    (batch: RunEvent[], live: boolean) => {
      const fresh = batch.filter((e) => typeof e.seq === "number" && !seen.current.has(e.seq));
      if (!fresh.length) return;
      for (const e of fresh) seen.current.add(e.seq);
      lastSeq.current = Math.max(lastSeq.current, ...fresh.map((e) => e.seq));
      setEvents((prev) => [...prev, ...fresh].sort((a, b) => a.seq - b.seq));
      if (live) {
        qc.setQueryData<RunDetail>(["run", id], (d) => (d ? fresh.reduce(applyEvent, d) : d));
        setLastEventAt(Date.now());
        scheduleRefetch();
      }
    },
    [qc, id, scheduleRefetch],
  );

  // Reset on run change
  useEffect(() => {
    setEvents([]);
    seen.current = new Set();
    lastSeq.current = 0;
    setStream("connecting");
  }, [id]);

  // Initial history
  const [historyLoaded, setHistoryLoaded] = useState(false);
  useEffect(() => {
    let alive = true;
    setHistoryLoaded(false);
    api
      .events(id, 0)
      .then((evs) => {
        if (!alive) return;
        ingest(Array.isArray(evs) ? evs : [], false);
      })
      .catch(() => {
        /* timeline shows empty state; the stream may still deliver */
      })
      .finally(() => alive && setHistoryLoaded(true));
    return () => {
      alive = false;
    };
  }, [id, ingest]);

  // Live stream (skipped for runs that were already finished when opened)
  useEffect(() => {
    if (!historyLoaded || !query.data) return;
    if (terminal) {
      setStream("closed");
      // Pick up trailing events published right after the terminal status.
      const t = setTimeout(() => {
        api
          .events(id, lastSeq.current)
          .then((evs) => ingest(Array.isArray(evs) ? evs : [], false))
          .catch(() => undefined);
        void qc.invalidateQueries({ queryKey: ["run", id] });
      }, 900);
      return () => clearTimeout(t);
    }
    const unsub = subscribeRun(id, lastSeq.current, {
      onEvent: (ev) => ingest([ev], true),
      onState: setStream,
    });
    return unsub;
    // re-subscribe only when the run id / terminal flag changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, historyLoaded, terminal, !!query.data]);

  useEffect(
    () => () => {
      if (refetchTimer.current) clearTimeout(refetchTimer.current);
    },
    [],
  );

  // While boosted on a finished run, pull new events too (the stream is closed for terminal runs).
  useEffect(() => {
    if (!terminal || Date.now() >= boostUntil) return;
    const iv = setInterval(() => {
      if (Date.now() >= boostUntil) {
        clearInterval(iv);
        setBoostUntil(0);
        return;
      }
      api
        .events(id, lastSeq.current)
        .then((evs) => ingest(Array.isArray(evs) ? evs : [], false))
        .catch(() => undefined);
    }, 1200);
    return () => clearInterval(iv);
  }, [terminal, boostUntil, id, ingest]);

  const boost = useCallback((ms = 15_000) => setBoostUntil(Date.now() + ms), []);

  return useMemo(
    () => ({
      detail: query.data,
      events,
      isLoading: query.isLoading,
      error: query.error,
      stream,
      refetch: () => void query.refetch(),
      boost,
      lastEventAt,
    }),
    [query.data, events, query.isLoading, query.error, stream, query, lastEventAt, boost],
  );
}
