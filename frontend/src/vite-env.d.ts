/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_MOCK?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare module "@mock" {
  import type { RunEvent } from "./lib/types";
  export function handle<T>(method: string, path: string, body?: unknown): Promise<T>;
  export function subscribe(runId: string, after: number, cb: (e: RunEvent) => void): () => void;
}
