import { useEffect, useRef } from "react";
import type { CustomerPage, Outcome } from "../types";
import { ago, countryFlag, num, stageTone, words } from "../format";
import { Button, CopyButton, Dash, Empty, Skeleton, Tag } from "./ui";

const COLUMNS = [
  "Customer & Contact",
  "Attribution",
  "Funnel Stage",
  "Downloaded",
  "Activated",
  "WhatsApp number",
  "Messages",
  "Last Activity",
  "First Seen",
];

export function CustomerTable({
  page,
  loading,
  search,
  outcome,
  onSearch,
  onOutcome,
  onOpen,
  onPage,
}: {
  page: CustomerPage | null;
  loading: boolean;
  search: string;
  outcome: Outcome;
  onSearch: (value: string) => void;
  onOutcome: (value: Outcome) => void;
  onOpen: (id: string) => void;
  onPage: (offset: number) => void;
}) {
  const searchRef = useRef<HTMLInputElement>(null);
  const rows = page?.rows ?? [];
  const total = page?.total ?? 0;
  const offset = page?.offset ?? 0;
  const filtered = Boolean(search.trim() || outcome);

  // Global '/' hotkey to focus search bar
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (
        e.key === "/" &&
        document.activeElement?.tagName !== "INPUT" &&
        document.activeElement?.tagName !== "TEXTAREA"
      ) {
        e.preventDefault();
        searchRef.current?.focus();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  function exportCsv() {
    if (!rows.length) return;
    const headers = [
      "ID",
      "Full Name",
      "Phone",
      "Source",
      "Campaign",
      "Sales Stage",
      "WhatsApp Number",
      "Downloaded At",
      "Activated At",
      "Created At",
    ];
    const csvRows = [
      headers.join(","),
      ...rows.map((r) =>
        [
          r.id,
          `"${(r.full_name ?? "").replace(/"/g, '""')}"`,
          `"+${r.phone}"`,
          r.source,
          `"${(r.campaign_name ?? "").replace(/"/g, '""')}"`,
          r.stage ?? "",
          `"${r.numbers.join(" | ")}"`,
          r.downloaded_at ?? "",
          r.activated_at ?? "",
          r.created_at,
        ].join(","),
      ),
    ];
    const blob = new Blob([csvRows.join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.setAttribute("download", `boomshare_customers_${new Date().toISOString().slice(0, 10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  const filterTabs: Array<{ id: Outcome; label: string; dotTone?: "emerald" | "teal" | "rose" }> = [
    { id: "", label: "All Customers" },
    { id: "activated", label: "Activated", dotTone: "emerald" },
    { id: "downloaded", label: "Downloaded", dotTone: "teal" },
    { id: "not_downloaded", label: "In Pipeline" },
    { id: "opted_out", label: "Opted Out", dotTone: "rose" },
  ];

  return (
    <section className="overflow-hidden rounded-xl border border-slate-200/80 bg-white/90 shadow-[0_4px_16px_-4px_rgba(0,0,0,0.05)] backdrop-blur-md dark:border-slate-800/80 dark:bg-[#111622]/90 dark:shadow-[0_4px_24px_-4px_rgba(0,0,0,0.3)]">
      {/* Search & Filter Header Bar */}
      <div className="border-b border-slate-100 p-4 dark:border-slate-800/80 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          {/* Quick Filter Pill Buttons */}
          <div className="flex flex-wrap items-center gap-1.5">
            {filterTabs.map((tab) => {
              const isActive = outcome === tab.id;
              return (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => onOutcome(tab.id)}
                  className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold tracking-tight transition-all cursor-pointer ${
                    isActive
                      ? "bg-slate-900 text-white shadow-sm dark:bg-emerald-500 dark:text-slate-950 font-bold"
                      : "border border-slate-200/80 bg-white text-slate-600 hover:bg-slate-50 hover:text-slate-900 dark:border-slate-700/60 dark:bg-slate-900/60 dark:text-slate-300 dark:hover:bg-slate-800"
                  }`}
                >
                  {tab.dotTone && (
                    <span
                      className={`size-1.5 rounded-full ${
                        tab.dotTone === "emerald"
                          ? "bg-emerald-500"
                          : tab.dotTone === "teal"
                            ? "bg-teal-400"
                            : "bg-rose-500"
                      }`}
                    />
                  )}
                  <span>{tab.label}</span>
                </button>
              );
            })}
          </div>

          {/* Right Action Bar: Total & Export */}
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-slate-400 tabular-nums dark:text-slate-500">
              {total === 1 ? "1 record" : `${num(total)} records`}
            </span>
            <Button size="sm" onClick={exportCsv} disabled={rows.length === 0}>
              <svg className="size-3.5 opacity-70" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
              <span>Export CSV</span>
            </Button>
          </div>
        </div>

        {/* Search row with shortcut hint */}
        <div className="relative flex items-center">
          <svg
            className="absolute left-3.5 size-4 text-slate-400 dark:text-slate-500 pointer-events-none"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            ref={searchRef}
            type="search"
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            placeholder="Search by customer name, telephone number, or country..."
            className="w-full rounded-lg border border-slate-200 bg-slate-50/60 py-2.5 pl-10 pr-20 text-xs font-medium outline-none transition-all focus:border-emerald-500 focus:bg-white focus:ring-3 focus:ring-emerald-500/15 dark:border-slate-700/80 dark:bg-slate-900/60 dark:text-slate-100 dark:focus:border-emerald-500 dark:focus:bg-slate-950"
          />
          <div className="absolute right-3 flex items-center gap-1.5 pointer-events-none">
            {search ? (
              <button
                type="button"
                onClick={() => onSearch("")}
                className="pointer-events-auto rounded p-0.5 text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
              >
                <svg className="size-3.5" viewBox="0 0 20 20" fill="currentColor">
                  <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
                </svg>
              </button>
            ) : (
              <kbd className="hidden rounded border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] font-semibold text-slate-400 shadow-xs sm:inline-block dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400">
                /
              </kbd>
            )}
          </div>
        </div>
      </div>

      {/* Modern High-Density Table */}
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-left text-xs">
          <thead>
            <tr className="border-b border-slate-200/90 bg-slate-50/80 dark:border-slate-800 dark:bg-slate-950/70">
              {COLUMNS.map((column) => (
                <th
                  key={column}
                  className="px-4 py-3 text-[10.5px] font-bold tracking-[0.07em] whitespace-nowrap text-slate-500 uppercase dark:text-slate-400"
                >
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800/60">
            {loading
              ? Array.from({ length: 8 }, (_, i) => (
                  <tr key={i}>
                    <td colSpan={COLUMNS.length} className="px-4 py-3.5">
                      <Skeleton className="h-5" />
                    </td>
                  </tr>
                ))
              : rows.map((row) => {
                  const flagInfo = countryFlag(row.phone);
                  const initial = (row.full_name ?? row.phone).charAt(0).toUpperCase();

                  return (
                    <tr
                      key={row.id}
                      tabIndex={0}
                      onClick={() => onOpen(row.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onOpen(row.id);
                        }
                      }}
                      className="group cursor-pointer transition-colors duration-150 hover:bg-emerald-50/30 focus:bg-emerald-50/40 focus:outline-none dark:hover:bg-emerald-950/10 dark:focus:bg-emerald-950/20"
                    >
                      {/* Customer Info with Avatar */}
                      <td className="px-4 py-3 whitespace-nowrap">
                        <div className="flex items-center gap-3">
                          <div className="grid size-8 shrink-0 place-items-center rounded-full bg-gradient-to-br from-emerald-500/20 to-teal-500/20 text-xs font-bold text-emerald-800 dark:from-emerald-500/30 dark:to-teal-500/30 dark:text-emerald-300 border border-emerald-500/20 shadow-xs">
                            {initial}
                          </div>
                          <div>
                            <div className="font-semibold text-slate-900 group-hover:text-emerald-700 dark:text-white dark:group-hover:text-emerald-400 flex items-center gap-1.5">
                              <span>{row.full_name ?? <Dash />}</span>
                              <span title={flagInfo.country} className="text-sm select-none">
                                {flagInfo.flag}
                              </span>
                            </div>
                            <div className="flex items-center gap-1 font-mono text-[11px] text-slate-400 dark:text-slate-500">
                              <span>+{row.phone}</span>
                              <CopyButton text={`+${row.phone}`} label="" />
                            </div>
                          </div>
                        </div>
                      </td>

                      {/* Attribution */}
                      <td className="px-4 py-3 whitespace-nowrap">
                        <Tag tone={row.source === "direct" ? "neutral" : "indigo"}>
                          {words(row.source)}
                        </Tag>
                        {row.campaign_name && (
                          <div className="mt-1 max-w-44 truncate text-[11px] font-medium text-slate-400 dark:text-slate-500" title={row.campaign_name}>
                            {row.campaign_name}
                          </div>
                        )}
                      </td>

                      {/* Funnel Stage */}
                      <td className="px-4 py-3 whitespace-nowrap">
                        {row.stage ? (
                          <Tag tone={stageTone(row.stage)} dot={row.stage === "activated" || row.stage === "human_handoff"}>
                            {words(row.stage)}
                          </Tag>
                        ) : (
                          <Dash />
                        )}
                      </td>

                      {/* Download Status */}
                      <td className="px-4 py-3 whitespace-nowrap">
                        {row.downloaded_at ? (
                          <span className="inline-flex items-center gap-1 text-[11.5px] font-semibold text-teal-600 dark:text-teal-400">
                            <svg className="size-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
                            </svg>
                            <span>{ago(row.downloaded_at)}</span>
                          </span>
                        ) : (
                          <Dash />
                        )}
                      </td>

                      {/* Activation Status */}
                      <td className="px-4 py-3 whitespace-nowrap">
                        {row.activated_at ? (
                          <span className="inline-flex items-center gap-1 rounded-md bg-emerald-500/10 px-2 py-0.5 text-[11.5px] font-bold text-emerald-700 dark:text-emerald-300 border border-emerald-500/20">
                            <svg className="size-3 text-emerald-500" fill="currentColor" viewBox="0 0 20 20">
                              <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                            </svg>
                            <span>Activated</span>
                          </span>
                        ) : row.opted_out_at ? (
                          <Tag tone="rose" dot>opted out</Tag>
                        ) : (
                          <Dash />
                        )}
                      </td>

                      {/* Which of our numbers they wrote to. Someone who
                          wrote to both is shown with both. */}
                      <td className="px-4 py-3 whitespace-nowrap">
                        {row.numbers.length === 0 ? (
                          <Dash />
                        ) : (
                          <span className="flex flex-wrap gap-1">
                            {row.numbers.map((label) => (
                              <Tag key={label} tone="neutral">
                                {label}
                              </Tag>
                            ))}
                          </span>
                        )}
                      </td>

                      {/* Messages Count */}
                      <td className="px-4 py-3 whitespace-nowrap tabular-nums font-semibold text-slate-700 dark:text-slate-300">
                        <span className="inline-flex items-center gap-1">
                          <svg className="size-3 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                          </svg>
                          <span>{num(row.messages)}</span>
                        </span>
                      </td>

                      {/* Last Activity */}
                      <td className="px-4 py-3 whitespace-nowrap text-slate-500 dark:text-slate-400 font-medium">
                        {ago(row.last_activity_at) ?? <Dash />}
                      </td>

                      {/* First Seen */}
                      <td className="px-4 py-3 whitespace-nowrap text-slate-400 dark:text-slate-500 font-medium">
                        <div className="flex items-center justify-between gap-2">
                          <span>{ago(row.created_at)}</span>
                          <span className="opacity-0 group-hover:opacity-100 transition-opacity text-emerald-600 dark:text-emerald-400 font-bold">
                            View →
                          </span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
          </tbody>
        </table>
      </div>

      {!loading && rows.length === 0 && (
        <Empty
          title={filtered ? "No records found" : "Pipeline is waiting for inbound leads"}
          detail={
            filtered
              ? "No customer matches your active filters. Try clearing your search or switching filter tabs."
              : "Customers will appear in real time once Meta Webhook receives new messages."
          }
        />
      )}

      {/* Pagination Footer */}
      <div className="flex items-center justify-between border-t border-slate-100 p-3.5 dark:border-slate-800/80">
        <span className="text-xs font-medium text-slate-400 tabular-nums dark:text-slate-500">
          {total === 0 ? "0 entries" : `Showing ${num(offset + 1)}–${num(offset + rows.length)} of ${num(total)} entries`}
        </span>
        <div className="flex items-center gap-1.5">
          <Button
            size="sm"
            onClick={() => onPage(Math.max(0, offset - (page?.limit ?? 50)))}
            disabled={offset === 0}
          >
            ← Previous
          </Button>
          <Button
            size="sm"
            onClick={() => onPage(offset + (page?.limit ?? 50))}
            disabled={offset + rows.length >= total}
          >
            Next →
          </Button>
        </div>
      </div>
    </section>
  );
}
