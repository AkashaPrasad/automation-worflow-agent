import type {
  AppConfig,
  ApprovalSubmission,
  Autonomy,
  Health,
  MemoryItem,
  Run,
  RunDetail,
  RunEvent,
  RunSummary,
  ToolSpec,
  WorkspaceSnapshot,
} from "./types";

export const MOCK = import.meta.env.VITE_MOCK === "1";
export const API_BASE = String(import.meta.env.VITE_API_BASE || "http://localhost:8000").replace(/\/+$/, "");

const WS_KEY = "adjutant.workspace";

function uuid(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

let cachedWorkspace: string | null = null;

/** Generated once per browser, kept in localStorage, sent as X-Workspace-Id on every call. */
export function workspaceId(): string {
  if (cachedWorkspace) return cachedWorkspace;
  try {
    const existing = localStorage.getItem(WS_KEY);
    if (existing && /^[A-Za-z0-9-]{8,64}$/.test(existing)) { // same rule the API enforces
      cachedWorkspace = existing;
      return existing;
    }
    const fresh = uuid();
    localStorage.setItem(WS_KEY, fresh);
    cachedWorkspace = fresh;
    return fresh;
  } catch {
    cachedWorkspace = uuid();
    return cachedWorkspace;
  }
}

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      // FastAPI validation errors: [{loc, msg, type}]
      return d
        .map((e) => {
          if (e && typeof e === "object" && "msg" in e) {
            const loc = Array.isArray((e as { loc?: unknown }).loc) ? (e as { loc: unknown[] }).loc.slice(1).join(".") : "";
            return `${loc ? `${loc}: ` : ""}${String((e as { msg: unknown }).msg)}`;
          }
          return String(e);
        })
        .join("; ");
    }
    return JSON.stringify(d);
  }
  return fallback;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  if (MOCK) {
    const mock = await import("@mock");
    return mock.handle<T>(method, path, body);
  }
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers: {
        "X-Workspace-Id": workspaceId(),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, `Can't reach the Adjutant API at ${API_BASE}. Is the backend running?`);
  }
  const text = await res.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!res.ok) {
    const fallback =
      res.status === 429
        ? "Rate limit reached. Try again in a little while."
        : `${res.status} ${res.statusText || "request failed"}`;
    throw new ApiError(res.status, detailOf(parsed, typeof parsed === "string" && parsed ? parsed : fallback));
  }
  return parsed as T;
}

export const api = {
  health: () => request<Health>("GET", "/api/health"),
  config: () => request<AppConfig>("GET", "/api/config"),
  tools: () => request<ToolSpec[]>("GET", "/api/tools"),

  listRuns: () => request<RunSummary[]>("GET", "/api/runs"),
  createRun: (req: string, autonomy: Autonomy) => request<Run>("POST", "/api/runs", { request: req, autonomy }),
  getRun: (id: string) => request<RunDetail>("GET", `/api/runs/${encodeURIComponent(id)}`),
  events: (id: string, after = 0) =>
    request<RunEvent[]>("GET", `/api/runs/${encodeURIComponent(id)}/events?after=${after}`),
  resolveApproval: (runId: string, approvalId: string, body: ApprovalSubmission) =>
    request<Run>(
      "POST",
      `/api/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`,
      body,
    ),
  clarify: (id: string, answer: string) =>
    request<Run>("POST", `/api/runs/${encodeURIComponent(id)}/clarify`, { answer }),
  control: (id: string, action: "pause" | "resume" | "cancel" | "rollback") =>
    request<Run>("POST", `/api/runs/${encodeURIComponent(id)}/${action}`),

  workspace: () => request<WorkspaceSnapshot>("GET", "/api/workspace"),
  resetWorkspace: () => request<WorkspaceSnapshot>("POST", "/api/workspace/reset"),

  memory: () => request<MemoryItem[]>("GET", "/api/memory"),
  deleteMemory: (id: string) => request<{ ok: boolean }>("DELETE", `/api/memory/${encodeURIComponent(id)}`),
};

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return String(err);
}

export function streamUrl(runId: string, after: number): string {
  const q = new URLSearchParams({ workspace_id: workspaceId(), after: String(after) });
  return `${API_BASE}/api/runs/${encodeURIComponent(runId)}/stream?${q.toString()}`;
}
