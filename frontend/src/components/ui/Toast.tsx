import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { CircleAlert, CircleCheck, Info, X } from "lucide-react";
import { cx } from "./primitives";

type ToastTone = "error" | "success" | "info";
interface ToastItem {
  id: number;
  tone: ToastTone;
  title: string;
  detail?: string;
}

interface ToastApi {
  push: (t: Omit<ToastItem, "id">) => void;
  error: (title: string, detail?: string) => void;
  success: (title: string, detail?: string) => void;
  info: (title: string, detail?: string) => void;
}

const Ctx = createContext<ToastApi | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);

  const dismiss = useCallback((id: number) => setItems((xs) => xs.filter((x) => x.id !== id)), []);

  const push = useCallback(
    (t: Omit<ToastItem, "id">) => {
      const id = ++seq.current;
      setItems((xs) => [...xs.slice(-3), { ...t, id }]);
      setTimeout(() => dismiss(id), t.tone === "error" ? 8000 : 4500);
    },
    [dismiss],
  );

  const api = useMemo<ToastApi>(
    () => ({
      push,
      error: (title, detail) => push({ tone: "error", title, detail }),
      success: (title, detail) => push({ tone: "success", title, detail }),
      info: (title, detail) => push({ tone: "info", title, detail }),
    }),
    [push],
  );

  return (
    <Ctx.Provider value={api}>
      {children}
      <div
        className="pointer-events-none fixed inset-x-0 bottom-0 z-[90] flex flex-col items-center gap-2 p-4 sm:items-end"
        role="region"
        aria-label="Notifications"
      >
        <div aria-live="polite" className="flex w-full flex-col items-center gap-2 sm:items-end">
          {items.map((t) => {
            const Icon = t.tone === "error" ? CircleAlert : t.tone === "success" ? CircleCheck : Info;
            return (
              <div
                key={t.id}
                role={t.tone === "error" ? "alert" : "status"}
                className="animate-rise pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-xl border border-line-strong bg-panel p-3 pr-2 shadow-pop"
              >
                <Icon
                  className={cx(
                    "mt-0.5 size-4 shrink-0",
                    t.tone === "error" ? "text-block-fg" : t.tone === "success" ? "text-auto-fg" : "text-accent",
                  )}
                  aria-hidden
                />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-ink">{t.title}</p>
                  {t.detail && <p className="mt-0.5 break-words text-xs text-ink-3">{t.detail}</p>}
                </div>
                <button
                  type="button"
                  onClick={() => dismiss(t.id)}
                  className="grid size-6 place-items-center rounded-md text-ink-3 hover:bg-panel-3 hover:text-ink"
                  aria-label="Dismiss notification"
                >
                  <X className="size-3.5" aria-hidden />
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </Ctx.Provider>
  );
}

export function useToast(): ToastApi {
  const v = useContext(Ctx);
  if (!v) throw new Error("useToast outside ToastProvider");
  return v;
}
