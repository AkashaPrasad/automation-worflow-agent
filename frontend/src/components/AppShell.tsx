import { NavLink, Outlet, useLocation } from "react-router";
import { Monitor, Moon, Sun } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";
import { useTheme } from "../lib/theme";
import { MOCK } from "../lib/api";
import { useHealth } from "../lib/queries";
import { cx } from "./ui/primitives";
import { Tooltip } from "./ui/Tooltip";

export function LogoMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={cx("size-6", className)} aria-hidden>
      <rect width="24" height="24" rx="6" fill="var(--inverse)" />
      <path d="M12 7.5v4M12 11.5 7.5 16M12 11.5l4.5 4.5" stroke="var(--inverse-ink)" strokeOpacity="0.55" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx="12" cy="7" r="2.25" fill="var(--inverse-ink)" />
      <circle cx="7.25" cy="16.5" r="2.25" fill="var(--inverse-ink)" />
      <circle cx="16.75" cy="16.5" r="2.25" fill="var(--ask-solid)" />
    </svg>
  );
}

const NAV = [
  { to: "/", label: "Command", end: true },
  { to: "/workspace", label: "Workspace" },
  { to: "/memory", label: "Memory" },
  { to: "/architecture", label: "How it works" },
];

function NavItems({ className }: { className?: string }) {
  const loc = useLocation();
  return (
    <nav aria-label="Primary" className={className}>
      {NAV.map((n) => {
        const runActive = n.to === "/" && loc.pathname.startsWith("/runs/");
        return (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.end}
            className={({ isActive }) =>
              cx(
                "relative inline-flex h-8 shrink-0 items-center rounded-md px-2.5 text-sm font-medium transition-colors duration-150",
                isActive || runActive ? "text-ink" : "text-ink-3 hover:bg-panel-3 hover:text-ink",
                (isActive || runActive) && "bg-panel-3",
              )
            }
          >
            {n.label}
          </NavLink>
        );
      })}
    </nav>
  );
}

function ThemeToggle() {
  const { pref, resolved, setPref } = useTheme();
  const next = pref === "system" ? (resolved === "dark" ? "light" : "dark") : pref === "dark" ? "light" : "system";
  const Icon = pref === "system" ? Monitor : resolved === "dark" ? Moon : Sun;
  const label = `Theme: ${pref === "system" ? `system (${resolved})` : pref}. Switch to ${next}.`;
  return (
    <Tooltip content={label} side="bottom">
      <button
        type="button"
        onClick={() => setPref(next)}
        aria-label={label}
        data-testid="theme-toggle"
        className="grid size-8 place-items-center rounded-md text-ink-3 transition-colors hover:bg-panel-3 hover:text-ink"
      >
        <Icon className="size-4" aria-hidden />
      </button>
    </Tooltip>
  );
}

function SystemChip() {
  const h = useHealth();
  const ok = h.data?.ok;
  const llm = h.data?.llm_configured;
  const tone = h.isError ? "bg-block-solid" : !h.data ? "bg-neutral-solid" : ok && llm ? "bg-auto-solid" : "bg-ask-solid";
  const text = h.isError ? "API offline" : !h.data ? "Connecting" : llm ? "Systems nominal" : "Planner key missing";
  return (
    <Tooltip
      side="bottom"
      content={
        h.data ? (
          <span className="block space-y-0.5">
            <span className="block">Planner: <span className="font-mono">{h.data.models.planner}</span></span>
            <span className="block">Judge: <span className="font-mono">{h.data.models.judge}</span> (fallback {h.data.models.fallback})</span>
            <span className="block">API v{h.data.version}</span>
          </span>
        ) : h.isError ? (
          "The API did not respond. Check VITE_API_BASE and that the backend is running."
        ) : (
          "Checking the API"
        )
      }
    >
      <span tabIndex={0} className="hidden h-7 items-center gap-2 rounded-md border border-line px-2 text-xs text-ink-2 sm:inline-flex">
        <span className={cx("size-1.5 rounded-full", tone)} aria-hidden />
        {text}
      </span>
    </Tooltip>
  );
}

export function AppShell({ children }: { children?: ReactNode }) {
  const loc = useLocation();
  const main = useRef<HTMLElement>(null);
  useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [loc.pathname]);

  return (
    <div className="flex min-h-dvh flex-col">
      <a
        href="#main"
        className="sr-only-focusable fixed left-3 top-3 z-[120] rounded-md bg-inverse px-3 py-2 text-sm font-medium text-inverse-ink"
      >
        Skip to content
      </a>
      <header className="sticky top-0 z-40 border-b border-line bg-[color-mix(in_oklch,var(--bg)_86%,transparent)] backdrop-blur-md">
        <div className="mx-auto flex h-12 max-w-[1480px] items-center gap-3 px-4 sm:px-6">
          <NavLink to="/" className="flex items-center gap-2 rounded-md pr-1" aria-label="Adjutant home">
            <LogoMark />
            <span className="text-[15px] font-semibold tracking-[-0.02em] text-ink">Adjutant</span>
          </NavLink>
          <div className="mx-1 hidden h-5 w-px bg-line md:block" aria-hidden />
          <NavItems className="hidden items-center gap-0.5 md:flex" />
          <div className="ml-auto flex items-center gap-1.5">
            {MOCK && (
              <Tooltip side="bottom" content="VITE_MOCK=1: the UI is running on in-browser fixtures. Nothing reaches a backend.">
                <span tabIndex={0} className="inline-flex h-6 items-center rounded-md border border-dashed border-shadow-line bg-shadow-bg px-2 text-2xs font-medium text-shadow-fg">
                  Mock data
                </span>
              </Tooltip>
            )}
            <SystemChip />
            <ThemeToggle />
          </div>
        </div>
        <NavItems className="scrollbar-none flex items-center gap-0.5 overflow-x-auto border-t border-line px-3 py-1 md:hidden" />
      </header>
      <main id="main" ref={main} tabIndex={-1} className="flex-1 outline-none">
        {children ?? <Outlet />}
      </main>
      <footer className="border-t border-line">
        <div className="mx-auto flex max-w-[1480px] flex-wrap items-center gap-x-4 gap-y-1 px-4 py-4 text-xs text-ink-3 sm:px-6">
          <span className="font-medium text-ink-2">Adjutant</span>
          <span>Muse plans. Jev judges. Code decides.</span>
          <span className="ml-auto">AIML Incubator showcase · Acme Robotics sandbox</span>
        </div>
      </footer>
    </div>
  );
}
