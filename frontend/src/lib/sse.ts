import { MOCK, streamUrl } from "./api";
import { TERMINAL_STATUSES, type RunEvent, type RunStatus } from "./types";

export type StreamState = "connecting" | "live" | "reconnecting" | "closed";

export interface StreamHandlers {
  onEvent: (ev: RunEvent) => void;
  onState?: (s: StreamState) => void;
}

/** True when this event means the run reached a terminal state (the server closes the stream after it). */
export function isTerminalEvent(ev: RunEvent): boolean {
  if (ev.type === "run.completed" || ev.type === "run.failed") return true;
  if (ev.type === "run.status") {
    const s = ev.data?.status as RunStatus | undefined;
    return !!s && TERMINAL_STATUSES.has(s);
  }
  return false;
}

/**
 * Subscribe to a run's live event stream.
 * - SSE via EventSource, event name `run_event`, resumes with `after=<lastSeq>` (and Last-Event-ID).
 * - Dedupes by seq, reconnects with capped backoff, stops after a terminal run.status.
 * Returns an unsubscribe function.
 */
export function subscribeRun(runId: string, after: number, handlers: StreamHandlers): () => void {
  let lastSeq = after;
  let closed = false;
  let es: EventSource | null = null;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  let mockUnsub: (() => void) | null = null;

  const deliver = (ev: RunEvent) => {
    if (closed || typeof ev?.seq !== "number") return;
    if (ev.seq <= lastSeq) return; // dedupe
    lastSeq = ev.seq;
    handlers.onEvent(ev);
    if (isTerminalEvent(ev)) {
      // Allow trailing events (summary, final effects) a moment to arrive, then stop.
      setTimeout(() => stop(), 1500);
    }
  };

  const stop = () => {
    if (closed) return;
    closed = true;
    if (retry) clearTimeout(retry);
    es?.close();
    mockUnsub?.();
    handlers.onState?.("closed");
  };

  if (MOCK) {
    handlers.onState?.("connecting");
    void import("@mock").then((m) => {
      if (closed) return;
      handlers.onState?.("live");
      mockUnsub = m.subscribe(runId, lastSeq, deliver);
    });
    return stop;
  }

  const connect = () => {
    if (closed) return;
    handlers.onState?.(attempt === 0 ? "connecting" : "reconnecting");
    es = new EventSource(streamUrl(runId, lastSeq));
    es.onopen = () => {
      attempt = 0;
      handlers.onState?.("live");
    };
    es.addEventListener("run_event", (msg) => {
      try {
        deliver(JSON.parse((msg as MessageEvent<string>).data) as RunEvent);
      } catch {
        /* ignore malformed frames */
      }
    });
    es.onerror = () => {
      if (closed) return;
      // Take over reconnection so the `after` cursor is always current.
      es?.close();
      attempt += 1;
      handlers.onState?.("reconnecting");
      const delay = Math.min(10_000, 500 * 2 ** Math.min(attempt, 5));
      retry = setTimeout(connect, delay);
    };
  };

  connect();
  return stop;
}
