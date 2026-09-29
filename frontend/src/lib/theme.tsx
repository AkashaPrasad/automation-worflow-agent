import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type ThemePref = "system" | "light" | "dark";
type Resolved = "light" | "dark";

const KEY = "adjutant.theme";

function systemDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches;
}

function readPref(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* storage blocked */
  }
  return "system";
}

const Ctx = createContext<{ pref: ThemePref; resolved: Resolved; setPref: (p: ThemePref) => void; cycle: () => void } | null>(
  null,
);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [pref, setPrefState] = useState<ThemePref>(readPref);
  const [sysDark, setSysDark] = useState<boolean>(systemDark);

  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const on = (e: MediaQueryListEvent) => setSysDark(e.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  const resolved: Resolved = pref === "system" ? (sysDark ? "dark" : "light") : pref;

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = resolved;
    root.style.colorScheme = resolved;
  }, [resolved]);

  const setPref = useCallback((p: ThemePref) => {
    setPrefState(p);
    try {
      localStorage.setItem(KEY, p);
    } catch {
      /* ignore */
    }
  }, []);

  const cycle = useCallback(() => {
    setPref(pref === "system" ? (sysDark ? "light" : "dark") : pref === "dark" ? "light" : "dark");
  }, [pref, sysDark, setPref]);

  const value = useMemo(() => ({ pref, resolved, setPref, cycle }), [pref, resolved, setPref, cycle]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useTheme outside ThemeProvider");
  return v;
}
