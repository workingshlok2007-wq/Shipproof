"use client";

import type { ButtonHTMLAttributes, ReactNode } from "react";
import { isSafeHttpUrl } from "@/lib/api";
import type { CheckStatus, Severity, SourceStatus } from "@/lib/types";

export function Card({ title, subtitle, action, children, className = "" }: {
  title?: string; subtitle?: string; action?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`clay p-6 ${className}`}>
      {(title || action) && (
        <header className="mb-4 flex items-start justify-between gap-4">
          <div>
            {title && <h2 className="text-base font-semibold tracking-tight">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

type Variant = "primary" | "quiet" | "danger";
const VARIANT: Record<Variant, string> = {
  primary: "bg-accent text-white hover:bg-accent-deep",
  quiet: "bg-surface text-ink hover:bg-sunken",
  danger: "bg-surface text-fail hover:bg-sunken",
};

export function Button({ variant = "quiet", className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return <button type="button" className={`clay-button px-4 py-2 text-sm font-medium ${VARIANT[variant]} ${className}`} {...props} />;
}

const STATUS_STYLE: Record<CheckStatus | SourceStatus, string> = {
  PASS: "bg-pass/12 text-pass",
  CHECKED: "bg-pass/12 text-pass",
  FAIL: "bg-fail/12 text-fail",
  UNREACHABLE: "bg-fail/12 text-fail",
  STALE: "bg-stale/15 text-stale",
  UNVERIFIED: "bg-unverified/12 text-unverified",
  NOT_CHECKED: "bg-unverified/12 text-unverified",
};

export function StatusBadge({ status }: { status: CheckStatus | SourceStatus }) {
  return (
    <span className={`inline-flex shrink-0 rounded-full px-2.5 py-0.5 text-[11px] font-semibold tracking-wide ${STATUS_STYLE[status]}`}>
      {status.replace("_", " ")}
    </span>
  );
}

const SEVERITY_STYLE: Record<Severity, string> = {
  CRITICAL: "bg-fail text-white",
  HIGH: "bg-fail/15 text-fail",
  MEDIUM: "bg-stale/15 text-stale",
  LOW: "bg-unverified/12 text-unverified",
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <span className={`inline-flex shrink-0 rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${SEVERITY_STYLE[severity]}`}>{severity}</span>;
}

/** Untrusted URLs: only http(s) become links, always opened without referrer or opener access. */
export function SafeLink({ href, children }: { href: string; children?: ReactNode }) {
  if (!isSafeHttpUrl(href)) return <span className="break-all font-mono text-xs">{href}</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="break-all font-mono text-xs text-accent-deep underline-offset-2 hover:underline">
      {children ?? href}
    </a>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block text-xs font-medium text-muted">
      <span className="mb-1 block">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] font-normal">{hint}</span>}
    </label>
  );
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null;
  return <p role="alert" className="clay-inset mt-3 px-3 py-2 text-xs text-fail">{message}</p>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="clay-inset px-4 py-3 text-xs text-muted">{children}</p>;
}

export function formatTime(value: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
