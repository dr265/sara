"use client";

import * as React from "react";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/* ── Ambient field: the single product-wide background ──────────────────── */
export function SaraField({ className }: { className?: string }) {
  return <div className={`sara-field sara-grain ${className ?? ""}`} aria-hidden="true" />;
}

/* ── Intersection-driven reveal, used by editorial sections ─────────────── */
export function useReveal(selector = ".sara-reveal") {
  React.useEffect(() => {
    if (typeof document === "undefined") return;
    const els = Array.from(
      document.querySelectorAll<HTMLElement>(selector),
    );
    if (!els.length) return;

    const reduce =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

    const io = new IntersectionObserver(
      (entries) =>
        entries.forEach((e) => {
          if (!e.isIntersecting) return;
          const el = e.target as HTMLElement;
          const delay = Number(el.dataset.delay ?? 0);
          if (delay && !reduce) el.style.transitionDelay = `${delay}ms`;
          el.classList.add("sara-visible");
          io.unobserve(el);
        }),
      { threshold: 0.12, rootMargin: "0px 0px -6% 0px" },
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, [selector]);
}

/* ── Shell ──────────────────────────────────────────────────────────────── */
export function Shell({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <div className={`sara-shell ${className ?? ""}`}>{children}</div>;
}

/* ── Eyebrow / kicker ──────────────────────────────────────────────────── */
export function Eyebrow({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span className={`sara-eyebrow ${className ?? ""}`}>
      {children}
    </span>
  );
}

/* ── Buttons ───────────────────────────────────────────────────────────── */
type ButtonVariant = "primary" | "ghost" | "quiet" | "danger";

export interface SaraButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
}

const buttonVariantClass: Record<ButtonVariant, string> = {
  primary: "sara-btn-primary",
  ghost: "sara-btn-ghost",
  quiet: "sara-btn-quiet",
  danger: "sara-btn-danger",
};

export const Button = React.forwardRef<HTMLButtonElement, SaraButtonProps>(
  function Button({ variant = "ghost", className, children, ...rest }, ref) {
    return (
      <button
        ref={ref}
        className={`sara-btn sara-focus-ring ${buttonVariantClass[variant]} ${className ?? ""}`}
        {...rest}
      >
        {children}
      </button>
    );
  },
);

/* ── Status pill ──────────────────────────────────────────────────────── */
type StatusTone = "ok" | "warn" | "err" | "info" | "idle";

const toneClass: Record<StatusTone, string> = {
  ok: "sara-status-ok",
  warn: "sara-status-warn",
  err: "sara-status-err",
  info: "sara-status-info",
  idle: "sara-status-idle",
};

export function StatusPill({
  tone = "idle",
  children,
  pulse = false,
  className,
}: {
  tone?: StatusTone;
  children: React.ReactNode;
  pulse?: boolean;
  className?: string;
}) {
  return (
    <span
      className={`sara-status ${toneClass[tone]} ${pulse ? "sara-pulse" : ""} ${className ?? ""}`}
    >
      <span className="sara-status-dot" aria-hidden="true" />
      {children}
    </span>
  );
}

/* ── Chip ─────────────────────────────────────────────────────────────── */
export function Chip({
  children,
  accent = false,
  className,
}: {
  children: React.ReactNode;
  accent?: boolean;
  className?: string;
}) {
  return (
    <span
      className={`sara-chip ${accent ? "sara-chip-accent" : ""} ${className ?? ""}`}
    >
      {children}
    </span>
  );
}

/* ── Panel: the default glass working surface ─────────────────────────── */
export function Panel({
  children,
  className,
  reveal = false,
  delay = 0,
  as: Tag = "div",
}: {
  children: React.ReactNode;
  className?: string;
  reveal?: boolean;
  delay?: number;
  as?: React.ElementType;
}) {
  return (
    <Tag
      {...(reveal ? { "data-reveal": true } : {})}
      {...(delay ? { "data-delay": delay } : {})}
      className={`sara-surface-raised sara-ring sara-scroll ${reveal ? "sara-reveal" : ""} ${className ?? ""}`}
    >
      {children}
    </Tag>
  );
}

/* ── Meter ────────────────────────────────────────────────────────────── */
export function Meter({
  value,
  className,
}: {
  value: number;
  className?: string;
}) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  return (
    <div className={`sara-meter ${className ?? ""}`} role="presentation">
      <span style={{ width: `${pct}%` }} />
    </div>
  );
}

/* ── Section heading block (eyebrow + serif/measured headline + body) ─── */
export function SectionHead({
  eyebrow,
  title,
  body,
  className,
  delay = 0,
}: {
  eyebrow?: string;
  title: React.ReactNode;
  body?: string;
  className?: string;
  delay?: number;
}) {
  return (
    <div
      className={`sara-reveal max-w-2xl ${className ?? ""}`}
      data-reveal
      data-delay={delay}
    >
      {eyebrow ? <Eyebrow>{eyebrow}</Eyebrow> : null}
      <h2 className="sara-display mt-4 text-[clamp(1.6rem,2.8vw,2.4rem)] leading-[1.06]">
        {title}
      </h2>
      {body ? (
        <p className="mt-4 text-[0.95rem] leading-relaxed text-[var(--sara-muted)]">
          {body}
        </p>
      ) : null}
    </div>
  );
}
