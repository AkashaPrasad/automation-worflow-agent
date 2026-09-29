import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Brain, Lightbulb, ListOrdered, RefreshCw, Star, Trash, User, type LucideIcon } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { useMemory } from "../../lib/queries";
import type { MemoryItem, MemoryKind } from "../../lib/types";
import { relative, str } from "../../lib/format";
import { Button, EmptyState, Mono, Skeleton, cx } from "../../components/ui/primitives";
import { useToast } from "../../components/ui/Toast";

const KIND: Record<MemoryKind, { label: string; icon: LucideIcon; cls: string }> = {
  fact: { label: "Fact", icon: Lightbulb, cls: "text-run-fg bg-run-bg border-run-line" },
  preference: { label: "Preference", icon: Star, cls: "text-ask-fg bg-ask-bg border-ask-line" },
  person: { label: "Person", icon: User, cls: "text-shadow-fg bg-shadow-bg border-shadow-line" },
  playbook: { label: "Playbook", icon: ListOrdered, cls: "text-auto-fg bg-auto-bg border-auto-line" },
};

export default function MemoryPage() {
  const q = useMemory();
  const qc = useQueryClient();
  const toast = useToast();
  const [filter, setFilter] = useState<MemoryKind | "all">("all");
  const del = useMutation({
    mutationFn: (id: string) => api.deleteMemory(id),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: ["memory"] });
      const prev = qc.getQueryData<MemoryItem[]>(["memory"]);
      qc.setQueryData<MemoryItem[]>(["memory"], (xs) => (xs ?? []).filter((m) => m.id !== id));
      return { prev };
    },
    onError: (e, _id, ctx) => {
      if (ctx?.prev) qc.setQueryData(["memory"], ctx.prev);
      toast.error("Couldn't delete that memory", errorMessage(e));
    },
    onSuccess: () => toast.success("Memory deleted", "Adjutant won't recall it in future runs."),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["memory"] }),
  });

  const items = q.data ?? [];
  const memories = useMemo(() => items.filter((m) => m.kind !== "playbook" && (filter === "all" || m.kind === filter)), [items, filter]);
  const playbooks = items.filter((m) => m.kind === "playbook");
  const counts = items.reduce<Record<string, number>>((acc, m) => ((acc[m.kind] = (acc[m.kind] ?? 0) + 1), acc), {});

  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-12 pt-6 sm:px-6">
      <h1 className="text-xl font-semibold tracking-[-0.02em] text-ink">Memory</h1>
      <p className="mt-1 max-w-[72ch] text-sm text-ink-2">
        What Adjutant learned from finished runs. At the start of every run Jev ranks these for relevance and only the useful ones reach the planner. Delete anything
        you don&rsquo;t want remembered.
      </p>

      {q.isLoading ? (
        <div className="mt-6 grid gap-4 lg:grid-cols-[1fr_400px]">
          <Skeleton className="h-96" />
          <Skeleton className="h-96" />
        </div>
      ) : q.isError ? (
        <div className="mt-6 rounded-xl border border-line bg-panel">
          <EmptyState icon={Brain} title="Couldn't load memory">
            {errorMessage(q.error)}
            <div className="mt-3">
              <Button size="sm" icon={RefreshCw} onClick={() => void q.refetch()}>
                Retry
              </Button>
            </div>
          </EmptyState>
        </div>
      ) : (
        <div className="mt-6 grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_400px]">
          <section className="overflow-hidden rounded-xl border border-line bg-panel shadow-card" aria-labelledby="mem-title">
            <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
              <h2 id="mem-title" className="text-sm font-semibold text-ink">
                Memories
              </h2>
              <div className="ml-auto flex flex-wrap gap-1" role="toolbar" aria-label="Filter memories">
                {(["all", "fact", "preference", "person"] as const).map((k) => (
                  <button
                    key={k}
                    type="button"
                    aria-pressed={filter === k}
                    onClick={() => setFilter(k)}
                    className={cx("inline-flex h-6 items-center gap-1.5 rounded-md px-2 text-2xs font-medium", filter === k ? "bg-panel-3 text-ink" : "text-ink-3 hover:text-ink")}
                  >
                    {k === "all" ? "All" : KIND[k].label}
                    <span className="tnum font-mono text-[10px] text-ink-4">{k === "all" ? items.length - playbooks.length : counts[k] ?? 0}</span>
                  </button>
                ))}
              </div>
            </header>
            {memories.length === 0 ? (
              <EmptyState icon={Brain} title={filter === "all" ? "Nothing remembered yet" : `No ${KIND[filter as MemoryKind].label.toLowerCase()} memories`}>
                When a run completes, Muse extracts facts, preferences and people worth keeping. <Link to="/" className="text-accent hover:underline">Start a run</Link> to see it learn.
              </EmptyState>
            ) : (
              <ul className="divide-y divide-line">
                {memories.map((m) => (
                  <MemoryRow key={m.id} m={m} onDelete={() => del.mutate(m.id)} deleting={del.isPending && del.variables === m.id} />
                ))}
              </ul>
            )}
          </section>

          <section className="overflow-hidden rounded-xl border border-line bg-panel shadow-card" aria-labelledby="pb-title">
            <header className="border-b border-line px-4 py-2.5">
              <h2 id="pb-title" className="text-sm font-semibold text-ink">
                Playbooks <span className="ml-1 font-mono text-2xs font-normal text-ink-3">{playbooks.length}</span>
              </h2>
              <p className="text-2xs text-ink-3">Plan outlines saved from successful runs, reused as a starting point for similar requests.</p>
            </header>
            {playbooks.length === 0 ? (
              <EmptyState icon={ListOrdered} title="No playbooks yet">
                A verified run saves its plan outline here.
              </EmptyState>
            ) : (
              <ul className="divide-y divide-line">
                {playbooks.map((p) => {
                  const outline = Array.isArray(p.data?.plan_outline) ? (p.data.plan_outline as unknown[]).map(str) : [];
                  return (
                    <li key={p.id} className="group px-4 py-3">
                      <div className="flex items-start gap-2">
                        <p className="min-w-0 flex-1 text-sm leading-snug text-ink">{str(p.data?.request) || p.text}</p>
                        <DeleteButton onClick={() => del.mutate(p.id)} busy={del.isPending && del.variables === p.id} />
                      </div>
                      {str(p.data?.request) && <p className="mt-1 text-xs text-ink-3">{p.text}</p>}
                      {outline.length > 0 && (
                        <ol className="mt-2 flex flex-wrap items-center gap-1">
                          {outline.map((t, i) => (
                            <li key={`${t}-${i}`} className="flex items-center gap-1">
                              <Mono className="rounded-[4px] border border-line bg-panel-2 px-1.5 py-px text-[10.5px] text-ink-2">{t}</Mono>
                              {i < outline.length - 1 && <ArrowRight className="size-3 text-ink-4" aria-hidden />}
                            </li>
                          ))}
                        </ol>
                      )}
                      <p className="mt-2 text-2xs text-ink-3">
                        {relative(p.created_at)} · used {p.uses}×
                        {p.source_run_id && (
                          <>
                            {" · "}
                            <Link to={`/runs/${p.source_run_id}`} className="text-accent hover:underline">
                              source run
                            </Link>
                          </>
                        )}
                      </p>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

function MemoryRow({ m, onDelete, deleting }: { m: MemoryItem; onDelete: () => void; deleting: boolean }) {
  const k = KIND[m.kind] ?? KIND.fact;
  return (
    <li className="group flex items-start gap-3 px-4 py-3" data-testid="memory-item" data-memory-id={m.id}>
      <span className="w-[92px] shrink-0">
        <span className={cx("mt-0.5 inline-flex h-5 items-center gap-1 rounded-[5px] border px-1.5 text-2xs font-medium", k.cls)}>
          <k.icon className="size-3" aria-hidden />
          {k.label}
        </span>
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-sm leading-snug text-ink">{m.text}</p>
        <p className="mt-1 text-2xs text-ink-3">
          {relative(m.created_at)} · recalled {m.uses}×
          {m.source_run_id && (
            <>
              {" · from "}
              <Link to={`/runs/${m.source_run_id}`} className="text-accent hover:underline">
                {m.source_run_id}
              </Link>
            </>
          )}
        </p>
      </div>
      <DeleteButton onClick={onDelete} busy={deleting} />
    </li>
  );
}

function DeleteButton({ onClick, busy }: { onClick: () => void; busy: boolean }) {
  return (
    <Button size="xs" variant="ghost" icon={Trash} onClick={onClick} loading={busy} aria-label="Delete memory" className="text-ink-3 hover:text-block-fg">
      <span className="sr-only sm:not-sr-only">Delete</span>
    </Button>
  );
}
