import { forwardRef, type ButtonHTMLAttributes, type HTMLAttributes, type ReactNode } from "react";
import { LoaderCircle, type LucideIcon } from "lucide-react";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

type Variant = "primary" | "secondary" | "ghost" | "danger" | "accent";
type Size = "xs" | "sm" | "md" | "lg";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-inverse text-inverse-ink border border-transparent hover:bg-[color-mix(in_oklch,var(--inverse)_86%,var(--bg))] active:translate-y-px",
  accent: "bg-accent text-accent-ink border border-transparent hover:brightness-110 active:translate-y-px",
  secondary:
    "bg-panel text-ink border border-line-strong hover:bg-panel-3 hover:border-[color-mix(in_oklch,var(--line-strong)_60%,var(--ink-4))] active:translate-y-px shadow-card",
  ghost: "bg-transparent text-ink-2 border border-transparent hover:bg-panel-3 hover:text-ink",
  danger: "bg-panel text-block-fg border border-block-line hover:bg-block-bg active:translate-y-px",
};

const SIZES: Record<Size, string> = {
  xs: "h-6 px-2 text-2xs gap-1 rounded-[5px]",
  sm: "h-7 px-2.5 text-xs gap-1.5 rounded-md",
  md: "h-8 px-3 text-sm gap-1.5 rounded-md",
  lg: "h-10 px-4 text-base gap-2 rounded-lg",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: LucideIcon;
  loading?: boolean;
  trailing?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", icon: Icon, loading, trailing, className, children, disabled, type = "button", ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cx(
        "inline-flex shrink-0 select-none items-center justify-center whitespace-nowrap font-medium transition-[background-color,border-color,color,transform,opacity] duration-150",
        "disabled:pointer-events-none disabled:opacity-45",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {loading ? (
        <LoaderCircle className="size-3.5 animate-spin" aria-hidden />
      ) : Icon ? (
        <Icon className={size === "lg" ? "size-4" : "size-3.5"} aria-hidden strokeWidth={2} />
      ) : null}
      {children}
      {trailing}
    </button>
  );
});

export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={cx(
        "inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-[4px] border border-line-strong bg-panel-2 px-1 font-mono text-[10px] font-medium text-ink-3",
        className,
      )}
    >
      {children}
    </kbd>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("skeleton", className)} aria-hidden />;
}

export function Panel({
  className,
  children,
  ...rest
}: HTMLAttributes<HTMLElement> & { children: ReactNode }) {
  return (
    <section className={cx("rounded-xl border border-line bg-panel shadow-card", className)} {...rest}>
      {children}
    </section>
  );
}

export function PanelHeader({
  title,
  icon: Icon,
  meta,
  actions,
  className,
  id,
}: {
  title: ReactNode;
  icon?: LucideIcon;
  meta?: ReactNode;
  actions?: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <header className={cx("flex min-h-11 flex-wrap items-center gap-x-3 gap-y-2 border-b border-line px-4 py-2", className)}>
      <h2 id={id} className="flex items-center gap-2 text-sm font-semibold text-ink">
        {Icon && <Icon className="size-4 text-ink-3" aria-hidden />}
        {title}
      </h2>
      {meta && <div className="text-xs text-ink-3">{meta}</div>}
      {actions && <div className="ml-auto flex items-center gap-1.5">{actions}</div>}
    </header>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  children,
  action,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cx("flex flex-col items-center justify-center px-6 py-10 text-center", className)}>
      {Icon && (
        <div className="mb-3 grid size-9 place-items-center rounded-lg border border-line bg-panel-2 text-ink-3">
          <Icon className="size-4" aria-hidden />
        </div>
      )}
      <p className="text-sm font-medium text-ink">{title}</p>
      {children && <div className="mt-1 max-w-[42ch] text-sm text-ink-3">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Mono({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  return (
    <span title={title} className={cx("font-mono text-[0.92em] tracking-[-0.01em]", className)}>
      {children}
    </span>
  );
}

export function Divider({ className }: { className?: string }) {
  return <div className={cx("h-px w-full bg-line", className)} role="presentation" />;
}

export function VisuallyHidden({ children, ...rest }: HTMLAttributes<HTMLSpanElement>) {
  return (
    <span className="sr-only" {...rest}>
      {children}
    </span>
  );
}
