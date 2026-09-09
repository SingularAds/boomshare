import type { Overview, Outcome } from "../types";
import { num, pct, words } from "../format";
import { Card, Empty, Skeleton } from "./ui";

/* -------------------------------------------------------------------------- */
/* Interactive Executive KPI cards                                            */
/* -------------------------------------------------------------------------- */
function KpiCard({
  label,
  value,
  secondary,
  icon,
  badge,
  badgeTone = "emerald",
  progressBar,
  active = false,
  onClick,
}: {
  label: string;
  value: number;
  secondary?: React.ReactNode;
  icon?: React.ReactNode;
  badge?: string;
  badgeTone?: "emerald" | "teal" | "indigo" | "amber";
  progressBar?: { value: number; max: number; label: string };
  active?: boolean;
  onClick?: () => void;
}) {
  const badgeStyles = {
    emerald: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/20",
    teal: "bg-teal-500/10 text-teal-700 dark:text-teal-300 border-teal-500/20",
    indigo: "bg-indigo-500/10 text-indigo-700 dark:text-indigo-300 border-indigo-500/20",
    amber: "bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/20",
  };

  return (
    <div
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      className={`group relative flex flex-col justify-between overflow-hidden rounded-xl border p-4 transition-all duration-200 select-none ${
        onClick ? "cursor-pointer hover:-translate-y-0.5 hover:shadow-md active:translate-y-0" : ""
      } ${
        active
          ? "border-emerald-500 bg-white ring-2 ring-emerald-500/20 shadow-md dark:border-emerald-500 dark:bg-[#141b2b] dark:ring-emerald-500/30"
          : "border-slate-200/85 bg-white/90 shadow-[0_2px_6px_-1px_rgba(0,0,0,0.04)] dark:border-slate-800/90 dark:bg-[#111622]/90 hover:border-slate-300 dark:hover:border-slate-700"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="text-[11px] font-bold tracking-[0.08em] text-slate-500 uppercase dark:text-slate-400">
          {label}
        </span>
        {icon && (
          <div className="text-slate-400 transition-colors group-hover:text-emerald-600 dark:text-slate-500 dark:group-hover:text-emerald-400">
            {icon}
          </div>
        )}
      </div>

      <div className="my-2.5 flex items-baseline gap-2.5">
        <span className="text-3xl font-extrabold tracking-tight tabular-nums text-slate-900 dark:text-white">
          {num(value)}
        </span>
        {badge && (
          <span
            className={`inline-block rounded-md border px-1.5 py-0.5 text-[11px] font-bold tracking-tight ${badgeStyles[badgeTone]}`}
          >
            {badge}
          </span>
        )}
      </div>

      {secondary && (
        <div className="text-[11.5px] text-slate-500 dark:text-slate-400 font-medium">
          {secondary}
        </div>
      )}

      {progressBar && (
        <div className="mt-3 pt-2 border-t border-slate-100 dark:border-slate-800/70">
          <div className="flex items-center justify-between text-[10.5px] font-medium text-slate-400 dark:text-slate-500 mb-1">
            <span>{progressBar.label}</span>
            <span className="tabular-nums font-semibold">{pct(progressBar.value, progressBar.max)}%</span>
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
            <div
              className="h-full rounded-full bg-gradient-to-r from-emerald-500 to-teal-400 transition-all duration-500"
              style={{ width: `${Math.min(100, pct(progressBar.value, progressBar.max))}%` }}
            />
          </div>
        </div>
      )}

      {onClick && (
        <div className="mt-2 text-[10px] font-semibold text-emerald-600 dark:text-emerald-400 opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1">
          <span>Click to filter</span>
          <span>→</span>
        </div>
      )}
    </div>
  );
}

export function KpiRow({
  data,
  selectedOutcome,
  onSelectOutcome,
}: {
  data: Overview;
  selectedOutcome?: Outcome;
  onSelectOutcome?: (outcome: Outcome) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
      <KpiCard
        label="Total Customers"
        value={data.customers}
        active={selectedOutcome === ""}
        onClick={() => onSelectOutcome?.("")}
        icon={
          <svg className="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.7} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
          </svg>
        }
        secondary={
          <span>
            <strong className="font-semibold text-slate-800 dark:text-slate-200">
              {num(data.customers_from_ads)}
            </strong>{" "}
            from ads ·{" "}
            <strong className="font-semibold text-slate-800 dark:text-slate-200">
              {num(data.customers_direct)}
            </strong>{" "}
            direct
          </span>
        }
      />

      <KpiCard
        label="Links Sent"
        value={data.links_sent}
        icon={
          <svg className="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.7} d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
          </svg>
        }
        badge={`${pct(data.customers_with_link, data.customers)}% reached`}
        badgeTone="indigo"
        secondary={
          <span>
            Distributed to{" "}
            <strong className="font-semibold text-slate-800 dark:text-slate-200">
              {num(data.customers_with_link)}
            </strong>{" "}
            unique leads
          </span>
        }
        progressBar={{
          value: data.customers_with_link,
          max: data.customers,
          label: "Offer Rate",
        }}
      />

  
      <KpiCard
        label="Conversations"
        value={data.conversations}
        icon={
          <svg className="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.7} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
          </svg>
        }
        badge={`${num(data.conversations_open)} open`}
        badgeTone="indigo"
        secondary={
          <span>
            Across{" "}
            <strong className="font-semibold text-slate-800 dark:text-slate-200">
              {num(data.customers)}
            </strong>{" "}
            customers, on every number they wrote to
          </span>
        }
      />
    </div>
  );
}

export function KpiSkeleton() {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {Array.from({ length: 3 }, (_, i) => (
        <div
          key={i}
          className="rounded-xl border border-slate-200/80 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-[#111622]"
        >
          <Skeleton className="h-3 w-24" />
          <Skeleton className="mt-3 h-8 w-28" />
          <Skeleton className="mt-2 h-3 w-36" />
        </div>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Interactive Stepped Conversion Funnel                                      */
/* -------------------------------------------------------------------------- */
export function Funnel({
  data,
  onSelectOutcome,
}: {
  data: Overview;
  onSelectOutcome?: (outcome: Outcome) => void;
}) {
  const top = data.customers;
  const steps: Array<{
    name: string;
    value: number;
    sublabel: string;
    outcome?: Outcome;
    tone: string;
  }> = [
    {
      name: "Customers reached",
      value: data.customers,
      sublabel: "Everyone who messaged us",
      outcome: "",
      tone: "from-slate-500 to-slate-600",
    },
    {
      name: "Sent a download link",
      value: data.customers_with_link,
      sublabel: "We sent them a tracked link",
      tone: "from-indigo-500 to-indigo-600",
    },
  ];

  if (top === 0) {
    return (
      <Empty
        title="No pipeline activity yet"
        detail="Metrics populate automatically as customers interact over WhatsApp."
      />
    );
  }

  return (
    <div className="space-y-3.5">
      {steps.map((step, index) => {
        const percentage = pct(step.value, top);
        const prevValue = index > 0 ? steps[index - 1].value : top;
        const stepDropoff = prevValue > 0 ? pct(step.value, prevValue) : 100;

        return (
          <div
            key={step.name}
            onClick={() => step.outcome !== undefined && onSelectOutcome?.(step.outcome)}
            className={`group rounded-lg p-2 transition-all ${
              step.outcome !== undefined
                ? "cursor-pointer hover:bg-slate-50/80 dark:hover:bg-slate-800/40"
                : ""
            }`}
          >
            <div className="mb-1.5 flex items-center justify-between gap-2 text-xs">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-slate-800 dark:text-slate-200">
                  {step.name}
                </span>
                <span className="text-[11px] text-slate-400 dark:text-slate-500">
                  ({step.sublabel})
                </span>
              </div>
              <div className="flex items-center gap-3">
                {index > 0 && (
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10.5px] font-semibold text-slate-500 dark:bg-slate-800 dark:text-slate-400">
                    {stepDropoff}% pass
                  </span>
                )}
                <span className="text-sm font-bold tabular-nums text-slate-900 dark:text-white">
                  {num(step.value)}
                </span>
                <span className="w-12 text-right text-xs font-semibold tabular-nums text-slate-400 dark:text-slate-500">
                  {percentage}%
                </span>
              </div>
            </div>

            <div className="relative h-2 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
              <div
                className={`h-full rounded-full bg-gradient-to-r ${step.tone} transition-all duration-500 shadow-sm`}
                style={{ width: `${Math.min(100, Math.max(3, percentage))}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Distribution lists                                                         */
/* -------------------------------------------------------------------------- */
export function Distribution({
  rows,
  accent = "emerald",
}: {
  rows: Array<[string, number]>;
  accent?: "emerald" | "indigo";
}) {
  const max = Math.max(...rows.map(([, v]) => v), 1);
  const barClass =
    accent === "indigo"
      ? "bg-gradient-to-r from-indigo-500 to-indigo-400"
      : "bg-gradient-to-r from-emerald-500 to-teal-400";

  return (
    <div className="space-y-2">
      {rows.map(([key, value]) => {
        const percentage = Math.round((value / max) * 100);
        return (
          <div
            key={key}
            className="flex items-center gap-3 rounded-lg px-2 py-1.5 text-xs transition-colors hover:bg-slate-50/80 dark:hover:bg-slate-800/30"
          >
            <span className="min-w-0 flex-1 truncate font-medium text-slate-700 capitalize dark:text-slate-300">
              {key}
            </span>
            <div className="h-1.5 w-28 shrink-0 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
              <div
                className={`h-full rounded-full ${barClass} transition-all duration-300`}
                style={{ width: `${percentage}%` }}
              />
            </div>
            <span className="w-10 text-right font-bold tabular-nums text-slate-900 dark:text-white">
              {num(value)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function Panels({
  data,
  onSelectOutcome,
}: {
  data: Overview;
  onSelectOutcome?: (outcome: Outcome) => void;
}) {
  const acquisition: Array<[string, number]> = data.leads_by_source.map((r) => [
    words(r.key),
    r.count,
  ]);
  if (data.customers_direct > 0) acquisition.push(["Direct (organic)", data.customers_direct]);

  return (
    <div className="space-y-3.5">
      <div className="grid grid-cols-1 gap-3.5 lg:grid-cols-[1.4fr_1fr]">
        <Card
          title="Conversion Funnel"
          subtitle="Only the steps we can verify from our own records"
        >
          <Funnel data={data} onSelectOutcome={onSelectOutcome} />
        </Card>

        <Card
          title="Attribution Channels"
          subtitle="Where inbound customers originated"
          action={
            <span className="rounded-md bg-emerald-500/10 px-2 py-0.5 text-[11px] font-bold text-emerald-700 dark:text-emerald-400">
              {num(data.campaigns)} Active Campaigns
            </span>
          }
        >
          {acquisition.length === 0 ? (
            <Empty
              title="No acquisition data"
              detail="Channel split renders once leads arrive via Meta or direct message."
            />
          ) : (
            <>
              <Distribution rows={acquisition} accent="emerald" />
              <div className="mt-4 pt-3 border-t border-slate-100 dark:border-slate-800/70 flex items-center justify-between text-[11px] text-slate-400 dark:text-slate-500">
                <span>Top channel: <strong>{acquisition[0]?.[0] ?? "None"}</strong></span>
                <span>{num(data.customers_from_ads)} total from paid ads</span>
              </div>
            </>
          )}
        </Card>
      </div>
    </div>
  );
}

export function PanelsSkeleton() {
  const block = (
    <div className="rounded-xl border border-slate-200/80 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-[#111622]">
      <Skeleton className="h-4 w-36" />
      <Skeleton className="mt-1 h-3 w-56" />
      <div className="mt-5 space-y-3">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="flex items-center gap-3">
            <Skeleton className="h-3 flex-1" />
            <Skeleton className="h-3 w-16" />
          </div>
        ))}
      </div>
    </div>
  );
  return (
    <div className="grid grid-cols-1 gap-3.5 lg:grid-cols-[1.4fr_1fr]">
      {block}
      {block}
    </div>
  );
}
