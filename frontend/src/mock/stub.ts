// Production stand-in for the mock backend (VITE_MOCK is off). Never called at runtime.
import type { RunEvent } from "../lib/types";

export async function handle<T>(): Promise<T> {
  throw new Error("mock backend is not bundled (set VITE_MOCK=1)");
}

export function subscribe(_runId: string, _after: number, _cb: (e: RunEvent) => void): () => void {
  return () => {};
}
