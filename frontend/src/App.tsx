import { lazy, Suspense, Component, type ReactNode } from "react";
import { BrowserRouter, Route, Routes, Link } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AppShell } from "./components/AppShell";
import { ThemeProvider } from "./lib/theme";
import { ToastProvider } from "./components/ui/Toast";
import { Skeleton } from "./components/ui/primitives";
import { LandingPage } from "./features/landing/LandingPage";
import { ApiError } from "./lib/api";

const RunPage = lazy(() => import("./features/run/RunPage"));
const WorkspacePage = lazy(() => import("./features/workspace/WorkspacePage"));
const MemoryPage = lazy(() => import("./features/memory/MemoryPage"));
const ArchitecturePage = lazy(() => import("./features/architecture/ArchitecturePage"));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
      refetchOnWindowFocus: false,
    },
  },
});

function PageFallback() {
  return (
    <div className="mx-auto max-w-[1480px] space-y-4 px-4 py-6 sm:px-6" aria-busy="true" aria-label="Loading">
      <Skeleton className="h-8 w-72" />
      <Skeleton className="h-16 w-full" />
      <div className="grid gap-4 lg:grid-cols-3">
        <Skeleton className="h-80 lg:col-span-2" />
        <Skeleton className="h-80" />
      </div>
    </div>
  );
}

function NotFound() {
  return (
    <div className="mx-auto flex max-w-lg flex-col items-center px-6 py-24 text-center">
      <p className="font-mono text-xs text-ink-3">404</p>
      <h1 className="mt-2 text-2xl font-semibold text-ink">Nothing at this address</h1>
      <p className="mt-2 text-sm text-ink-3">The page may have moved, or the link is incomplete.</p>
      <Link to="/" className="mt-6 inline-flex h-8 items-center rounded-md bg-inverse px-3 text-sm font-medium text-inverse-ink">
        Back to Command
      </Link>
    </div>
  );
}

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="mx-auto max-w-lg px-6 py-24 text-center" role="alert">
        <h1 className="text-xl font-semibold text-ink">Something broke on this screen</h1>
        <p className="mt-2 break-words font-mono text-xs text-ink-3">{this.state.error.message}</p>
        <button
          type="button"
          onClick={() => {
            this.setState({ error: null });
            window.location.reload();
          }}
          className="mt-6 inline-flex h-8 items-center rounded-md border border-line-strong bg-panel px-3 text-sm font-medium text-ink"
        >
          Reload
        </button>
      </div>
    );
  }
}

export function App() {
  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <BrowserRouter>
            <ErrorBoundary>
              <Routes>
                <Route element={<AppShell />}>
                  <Route index element={<LandingPage />} />
                  <Route path="runs/:id" element={<Suspense fallback={<PageFallback />}><RunPage /></Suspense>} />
                  <Route path="workspace" element={<Suspense fallback={<PageFallback />}><WorkspacePage /></Suspense>} />
                  <Route path="memory" element={<Suspense fallback={<PageFallback />}><MemoryPage /></Suspense>} />
                  <Route path="architecture" element={<Suspense fallback={<PageFallback />}><ArchitecturePage /></Suspense>} />
                  <Route path="*" element={<NotFound />} />
                </Route>
              </Routes>
            </ErrorBoundary>
          </BrowserRouter>
        </ToastProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
