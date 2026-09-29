import { forwardRef, useImperativeHandle, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, TriangleAlert } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import type { AppConfig, Autonomy, AutonomyLevel } from "../../lib/types";
import { Button, cx } from "../../components/ui/primitives";
import { useToast } from "../../components/ui/Toast";

const FALLBACK_LEVELS: AutonomyLevel[] = [
  { id: "cautious", title: "Cautious", description: "Every write asks for your approval before it runs." },
  { id: "balanced", title: "Balanced", description: "Low-risk reversible writes run on their own; messages to others ask unless clearly safe." },
  { id: "autonomous", title: "Autonomous", description: "Only risky, irreversible, or untrusted-content actions ask." },
];

function levelsFrom(cfg: AppConfig | undefined): AutonomyLevel[] {
  const raw = cfg?.autonomy_levels;
  if (!Array.isArray(raw) || !raw.length) return FALLBACK_LEVELS;
  return raw.map((l) => {
    if (typeof l === "string") return FALLBACK_LEVELS.find((f) => f.id === l) ?? { id: l as Autonomy, title: l, description: "" };
    return { ...FALLBACK_LEVELS.find((f) => f.id === l.id), ...l } as AutonomyLevel;
  });
}

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.userAgent);

export interface ComposerHandle {
  fill: (text: string) => void;
}

export const Composer = forwardRef<ComposerHandle, { config: AppConfig | undefined; llmMissing: boolean }>(function Composer(
  { config, llmMissing },
  ref,
) {
  const [text, setText] = useState("");
  const [autonomy, setAutonomy] = useState<Autonomy>("balanced");
  const [flash, setFlash] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const navigate = useNavigate();
  const toast = useToast();
  const qc = useQueryClient();
  const levels = useMemo(() => levelsFrom(config), [config]);
  const current = levels.find((l) => l.id === autonomy) ?? levels[1];

  useImperativeHandle(ref, () => ({
    fill: (t: string) => {
      setText(t);
      setFlash(true);
      setTimeout(() => setFlash(false), 700);
      requestAnimationFrame(() => {
        area.current?.focus();
        area.current?.setSelectionRange(t.length, t.length);
        area.current?.scrollIntoView({ block: "center", behavior: "smooth" });
      });
    },
  }));

  const create = useMutation({
    mutationFn: () => api.createRun(text.trim(), autonomy),
    onSuccess: (run) => {
      void qc.invalidateQueries({ queryKey: ["runs"] });
      navigate(`/runs/${run.id}`);
    },
    onError: (e) => toast.error("Couldn't start the run", errorMessage(e)),
  });

  const canSubmit = text.trim().length > 0 && !create.isPending;
  const submit = () => canSubmit && create.mutate();

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      className={cx(
        "rounded-xl border bg-panel shadow-card transition-[border-color,box-shadow] duration-300",
        flash ? "border-accent-line shadow-pop" : "border-line-strong focus-within:border-accent-line",
      )}
      aria-label="Start a run"
    >
      {llmMissing && (
        <div role="alert" className="flex items-start gap-2 rounded-t-xl border-b border-ask-line bg-ask-bg px-4 py-2.5 text-sm text-ask-fg">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          <p>
            <strong className="font-semibold">Planner model key not configured.</strong> Runs will stop at planning until{" "}
            <code className="font-mono text-xs">MODEL_API_KEY</code> is set on the backend.
          </p>
        </div>
      )}
      <label htmlFor="composer" className="sr-only">
        What should Adjutant handle?
      </label>
      <textarea
        id="composer"
        ref={area}
        data-testid="composer-input"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            submit();
          }
        }}
        rows={4}
        maxLength={8000}
        placeholder="Recap the Northwind QBR, email everyone who attended, and book a 30-minute follow-up next week when all of us are free."
        className="block min-h-[120px] w-full resize-y rounded-t-xl bg-transparent px-4 pb-2 pt-4 text-md leading-relaxed text-ink outline-none"
      />
      <div className="flex flex-col gap-3 border-t border-line px-3 py-3 sm:flex-row sm:items-center">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <label htmlFor="autonomy" className="shrink-0 text-xs font-medium text-ink-3">
            Autonomy
          </label>
          <div className="relative shrink-0">
            <select
              id="autonomy"
              data-testid="autonomy-select"
              value={autonomy}
              onChange={(e) => setAutonomy(e.target.value as Autonomy)}
              aria-describedby="autonomy-help"
              className="h-8 appearance-none rounded-md border border-line-strong bg-panel pl-2.5 pr-8 text-sm font-medium text-ink shadow-card outline-none transition-colors hover:bg-panel-3 focus-visible:border-accent"
            >
              {levels.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.title}
                </option>
              ))}
            </select>
            <svg aria-hidden viewBox="0 0 16 16" className="pointer-events-none absolute right-2.5 top-1/2 size-3.5 -translate-y-1/2 text-ink-3">
              <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <p id="autonomy-help" className="min-w-0 text-xs leading-snug text-ink-3" aria-live="polite">
            {current?.description}
          </p>
        </div>
        <Button
          type="submit"
          variant="primary"
          size="md"
          data-testid="composer-submit"
          disabled={!canSubmit}
          loading={create.isPending}
          className="w-full sm:w-auto"
          trailing={
            <span className="ml-1 hidden font-mono text-[11px] font-normal opacity-60 sm:inline" aria-hidden>
              {isMac ? "⌘↵" : "Ctrl ↵"}
            </span>
          }
        >
          {create.isPending ? "Starting" : "Start shadow run"}
          {!create.isPending && <ArrowRight className="size-3.5" aria-hidden />}
        </Button>
      </div>
    </form>
  );
});
