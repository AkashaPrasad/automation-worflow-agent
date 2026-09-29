import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { cx } from "./primitives";

/**
 * Accessible tooltip rendered in a portal (so it escapes React Flow and scroll containers).
 * Opens on hover and keyboard focus, closes on Escape. The trigger gets aria-describedby.
 */
export function Tooltip({
  content,
  children,
  side = "top",
  className,
  maxWidth = 280,
}: {
  content: ReactNode;
  children: ReactNode;
  side?: "top" | "bottom";
  className?: string;
  maxWidth?: number;
}) {
  const id = useId();
  const trigger = useRef<HTMLSpanElement>(null);
  const tip = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number; flip: boolean }>({ left: 0, top: 0, flip: false });
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const place = useCallback(() => {
    const el = trigger.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const tipH = tip.current?.offsetHeight ?? 60;
    const tipW = tip.current?.offsetWidth ?? maxWidth;
    let flip = side === "top" ? r.top - tipH - 8 < 4 : r.bottom + tipH + 8 > window.innerHeight - 4;
    if (side === "bottom" && flip && r.top - tipH - 8 < 4) flip = false;
    const wantTop = side === "top" ? !flip : flip;
    const top = wantTop ? r.top - tipH - 8 : r.bottom + 8;
    const half = tipW / 2;
    const left = Math.min(Math.max(r.left + r.width / 2, half + 8), window.innerWidth - half - 8);
    setPos({ left, top, flip });
  }, [side, maxWidth]);

  const show = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setOpen(true), 120);
  };
  const hide = () => {
    if (timer.current) clearTimeout(timer.current);
    setOpen(false);
  };

  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onScroll = () => setOpen(false);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [open]);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  return (
    <span
      ref={trigger}
      className={cx("inline-flex", className)}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
      aria-describedby={open ? id : undefined}
    >
      {children}
      {open &&
        createPortal(
          <div
            ref={tip}
            id={id}
            role="tooltip"
            style={{ left: pos.left, top: pos.top, maxWidth }}
            className="animate-fade pointer-events-none fixed z-[100] -translate-x-1/2 rounded-lg border border-line-strong bg-panel px-3 py-2 text-xs leading-relaxed text-ink-2 shadow-pop"
          >
            {content}
          </div>,
          document.body,
        )}
    </span>
  );
}
