import { useState, type ReactNode } from "react";

/** Shared executive UI primitives. */

export function Card({
  title,
  subtitle,
  action,
  children,
  className = "",
}: {
  title?: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`relative overflow-hidden rounded-xl border border-slate-200/80 bg-white/90 shadow-[0_2px_8px_-2px_rgba(0,0,0,0.05)] backdrop-blur-md transition-all duration-200 dark:border-slate-800/80 dark:bg-[#111622]/90 dark:shadow-[0_4px_16px_-4px_rgba(0,0,0,0.3)] ${className}`}
    >
      {(title || action) && (
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3.5 dark:border-slate-800/80">
          <div>
            {title && (
              <h2 className="text-[11.5px] font-bold tracking-[0.08em] text-slate-500 uppercase dark:text-slate-400">
                {title}
              </h2>
            )}
            {subtitle && (
              <p className="mt-0.5 text-[11px] text-slate-400 dark:text-slate-500">{subtitle}</p>
            )}
          </div>
          {action && <div className="flex items-center gap-2">{action}</div>}
        </div>
      )}
      <div className={title || action ? "p-5" : ""}>{children}</div>
    </section>
  );
}

const TONES = {
  emerald:
    "bg-emerald-500/10 text-emerald-700 border-emerald-500/20 dark:bg-emerald-500/15 dark:text-emerald-300 dark:border-emerald-500/30",
  teal:
    "bg-teal-500/10 text-teal-700 border-teal-500/20 dark:bg-teal-500/15 dark:text-teal-300 dark:border-teal-500/30",
  indigo:
    "bg-indigo-500/10 text-indigo-700 border-indigo-500/20 dark:bg-indigo-500/15 dark:text-indigo-300 dark:border-indigo-500/30",
  amber:
    "bg-amber-500/10 text-amber-700 border-amber-500/20 dark:bg-amber-500/15 dark:text-amber-300 dark:border-amber-500/30",
  rose:
    "bg-rose-500/10 text-rose-700 border-rose-500/20 dark:bg-rose-500/15 dark:text-rose-300 dark:border-rose-500/30",
  neutral:
    "bg-slate-100 text-slate-600 border-slate-200 dark:bg-slate-800/80 dark:text-slate-300 dark:border-slate-700/60",
} as const;

export function Tag({
  children,
  tone = "neutral",
  dot = false,
  className = "",
}: {
  children: ReactNode;
  tone?: keyof typeof TONES;
  dot?: boolean;
  className?: string;
}) {
  const dotColors = {
    emerald: "bg-emerald-500",
    teal: "bg-teal-500",
    indigo: "bg-indigo-500",
    amber: "bg-amber-500",
    rose: "bg-rose-500",
    neutral: "bg-slate-400",
  };

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11.5px] font-semibold tracking-wide whitespace-nowrap transition-colors ${TONES[tone]} ${className}`}
    >
      {dot && <span className={`size-1.5 rounded-full ${dotColors[tone]}`} />}
      {children}
    </span>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div
      className={`shimmer relative overflow-hidden rounded-lg bg-slate-200/70 dark:bg-slate-800/60 ${className}`}
    />
  );
}

export function Empty({
  title,
  detail,
  icon,
}: {
  title: string;
  detail: string;
  icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center px-5 py-12 text-center">
      {icon ? (
        <div className="mb-3 text-slate-400 dark:text-slate-500">{icon}</div>
      ) : (
        <div className="mb-3 grid size-10 place-items-center rounded-full bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500">
          <svg className="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" />
          </svg>
        </div>
      )}
      <p className="font-semibold text-slate-700 dark:text-slate-200">{title}</p>
      <p className="mx-auto mt-1 max-w-sm text-xs leading-relaxed text-slate-500 dark:text-slate-400">
        {detail}
      </p>
    </div>
  );
}

export const Dash = () => <span className="text-slate-300 dark:text-slate-600">—</span>;

export function Button({
  children,
  onClick,
  variant = "default",
  size = "md",
  disabled,
  type = "button",
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "default" | "primary" | "ghost" | "danger";
  size?: "sm" | "md";
  disabled?: boolean;
  type?: "button" | "submit";
  className?: string;
}) {
  const sizeClasses = size === "sm" ? "px-2.5 py-1.5 text-xs" : "px-3.5 py-2 text-[13px]";
  const base =
    "inline-flex items-center justify-center gap-1.5 rounded-lg font-semibold tracking-tight transition-all duration-150 active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed disabled:active:scale-100 shadow-sm cursor-pointer";

  let styles = "";
  if (variant === "primary") {
    styles =
      "bg-emerald-600 text-white hover:bg-emerald-500 shadow-emerald-950/10 dark:bg-emerald-500 dark:text-slate-950 dark:hover:bg-emerald-400 font-bold";
  } else if (variant === "ghost") {
    styles =
      "border-transparent bg-transparent text-slate-600 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800 shadow-none";
  } else if (variant === "danger") {
    styles =
      "border border-rose-200 bg-rose-50 text-rose-700 hover:bg-rose-100 dark:border-rose-900/60 dark:bg-rose-950/40 dark:text-rose-300";
  } else {
    styles =
      "border border-slate-200/90 bg-white text-slate-700 hover:bg-slate-50 hover:border-slate-300 dark:border-slate-700/80 dark:bg-slate-900/80 dark:text-slate-200 dark:hover:bg-slate-800 dark:hover:border-slate-600";
  }

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`${base} ${sizeClasses} ${styles} ${className}`}
    >
      {children}
    </button>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  async function handleCopy(e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // fallback
    }
  }

  return (
    <button
      type="button"
      onClick={handleCopy}
      title="Copy to clipboard"
      className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200"
    >
      {copied ? (
        <>
          <svg className="size-3 text-emerald-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
          </svg>
          <span className="text-emerald-600 dark:text-emerald-400 font-semibold">Copied!</span>
        </>
      ) : (
        <>
          <svg className="size-3 opacity-70" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
          </svg>
          <span>{label}</span>
        </>
      )}
    </button>
  );
}
