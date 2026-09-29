import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { TERMINAL_STATUSES } from "./types";

export function useHealth() {
  return useQuery({ queryKey: ["health"], queryFn: api.health, staleTime: 30_000, retry: 1, refetchInterval: 60_000 });
}

export function useConfig() {
  return useQuery({ queryKey: ["config"], queryFn: api.config, staleTime: 5 * 60_000, retry: 1 });
}

export function useTools() {
  return useQuery({ queryKey: ["tools"], queryFn: api.tools, staleTime: 5 * 60_000, retry: 1 });
}

export function useRuns() {
  return useQuery({
    queryKey: ["runs"],
    queryFn: api.listRuns,
    refetchInterval: (q) => (q.state.data?.some((r) => !TERMINAL_STATUSES.has(r.status)) ? 5000 : 30_000),
  });
}

export function useWorkspace() {
  return useQuery({ queryKey: ["workspace"], queryFn: api.workspace, staleTime: 5000 });
}

export function useMemory() {
  return useQuery({ queryKey: ["memory"], queryFn: api.memory });
}
