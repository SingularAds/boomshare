import { useEffect, useState } from "react";
import type { CustomerDetail } from "../types";
import { countryFlag, dateTime, stageTone, words } from "../format";
import { ChatThread } from "./ChatThread";
import { CopyButton, Dash, Skeleton, Tag } from "./ui";

function FactCard({
  label,
  value,
  action,
}: {
  label: string;
  value: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-slate-100 bg-slate-50/60 p-3 dark:border-slate-800/80 dark:bg-slate-900/50">
      <div className="flex items-center justify-between">
        <span className="text-[10.5px] font-bold tracking-[0.06em] text-slate-400 uppercase dark:text-slate-500">
          {label}
        </span>
        {action}
      </div>
      <div className="mt-1 text-xs font-semibold text-slate-900 dark:text-white">{value}</div>
    </div>
  );
}

function LoadingBody() {
  return (
    <div className="chat-surface flex-1 space-y-3 overflow-hidden p-5">
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} className={`flex ${i % 2 ? "justify-end" : "justify-start"}`}>
          <div style={{ width: `${160 + ((i * 47) % 220)}px` }} className="h-12">
            <Skeleton className="h-full rounded-xl" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function CustomerModal({
  customer,
  loading,
  error,
  onClose,
}: {
  customer: CustomerDetail | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
}) {
  const [activeTab, setActiveTab] = useState<"chat" | "attribution" | "profile">("chat");
  const [activeThreadIndex, setActiveThreadIndex] = useState(0);

  useEffect(() => {
    setActiveTab("chat");
    setActiveThreadIndex(0);
  }, [customer?.id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const name = customer?.full_name ?? (loading ? "Loading Contact Details…" : "Unknown Contact");
  const initial = (customer?.full_name ?? customer?.phone ?? "?").trim().charAt(0).toUpperCase();
  const flagInfo = customer ? countryFlag(customer.phone) : { flag: "🌐", country: "Global" };
  const currentThread = customer?.conversations?.[activeThreadIndex];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 p-3 sm:p-5 backdrop-blur-md transition-opacity"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Customer details for ${name}`}
        className="flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-slate-200/90 bg-white shadow-2xl dark:border-slate-800 dark:bg-[#111622] transition-transform duration-200"
      >
        {/* Header */}
        <header className="border-b border-slate-100 p-4 sm:p-5 dark:border-slate-800/90">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-center gap-3.5">
              <div className="grid size-12 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-emerald-500/20 to-teal-500/20 text-lg font-bold text-emerald-800 dark:from-emerald-500/30 dark:to-teal-500/30 dark:text-emerald-300 border border-emerald-500/30 shadow-xs">
                {initial}
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <h2 className="text-base font-bold text-slate-900 dark:text-white">{name}</h2>
                  <span title={flagInfo.country} className="text-base select-none">
                    {flagInfo.flag}
                  </span>
                </div>
                {customer && (
                  <div className="mt-0.5 flex items-center gap-2 text-xs font-mono text-slate-500 dark:text-slate-400">
                    <span>+{customer.phone}</span>
                    <CopyButton text={`+${customer.phone}`} label="Copy" />
                    <span className="text-slate-300 dark:text-slate-700">·</span>
                    <a
                      href={`https://wa.me/${customer.phone}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 font-sans text-emerald-600 hover:text-emerald-500 dark:text-emerald-400 font-semibold"
                    >
                      <span>Open WhatsApp</span>
                      <span className="text-[10px]">↗</span>
                    </a>
                  </div>
                )}
              </div>
            </div>

            {/* Redesigned Sleek Close Button */}
            <button
              type="button"
              onClick={onClose}
              aria-label="Close dialog"
              className="group flex size-8 items-center justify-center rounded-lg border border-slate-200 bg-slate-50 text-slate-500 transition-all hover:border-slate-300 hover:bg-slate-100 hover:text-slate-800 dark:border-slate-700/80 dark:bg-slate-800/60 dark:text-slate-400 dark:hover:border-slate-600 dark:hover:bg-slate-700 dark:hover:text-white cursor-pointer shadow-2xs"
            >
              <svg className="size-4 transition-transform group-hover:scale-110" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* Quick Badges & Navigation Tabs */}
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 pt-3 dark:border-slate-800/60">
            {/* Tabs */}
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                onClick={() => setActiveTab("chat")}
                className={`rounded-lg px-3 py-1 text-xs font-bold transition-all cursor-pointer ${
                  activeTab === "chat"
                    ? "bg-slate-900 text-white dark:bg-emerald-500 dark:text-slate-950"
                    : "text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
                }`}
              >
                Chat History {customer && `(${currentThread?.messages?.length ?? 0})`}
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("attribution")}
                className={`rounded-lg px-3 py-1 text-xs font-bold transition-all cursor-pointer ${
                  activeTab === "attribution"
                    ? "bg-slate-900 text-white dark:bg-emerald-500 dark:text-slate-950"
                    : "text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
                }`}
              >
                Downloads & Links {customer && `(${customer.links?.length ?? 0})`}
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("profile")}
                className={`rounded-lg px-3 py-1 text-xs font-bold transition-all cursor-pointer ${
                  activeTab === "profile"
                    ? "bg-slate-900 text-white dark:bg-emerald-500 dark:text-slate-950"
                    : "text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
                }`}
              >
                Lead Profile
              </button>
            </div>

            {/* Right Status Tags */}
            {customer && (
              <div className="flex flex-wrap items-center gap-1.5">
                <Tag tone={customer.source === "direct" ? "neutral" : "indigo"}>
                  {words(customer.source)}
                </Tag>
                {currentThread?.sales_stage && (
                  <Tag tone={stageTone(currentThread.sales_stage)} dot>
                    {words(currentThread.sales_stage)}
                  </Tag>
                )}
                {customer.activated_at && <Tag tone="emerald" dot>Activated</Tag>}
              </div>
            )}
          </div>
        </header>

        {/* Modal Body */}
        <div className="flex flex-1 flex-col overflow-hidden min-h-80">
          {error && (
            <div className="m-4 rounded-xl border border-rose-200 bg-rose-50 p-4 text-xs font-semibold text-rose-700 dark:border-rose-900/60 dark:bg-rose-950/40 dark:text-rose-300">
              {error}
            </div>
          )}

          {loading ? (
            <LoadingBody />
          ) : activeTab === "chat" ? (
            <div className="flex flex-1 flex-col overflow-hidden">
              {/* Thread switcher if multiple */}
              {(customer?.conversations?.length ?? 0) === 1 && currentThread && (
                <div className="flex items-center gap-2 border-b border-slate-100 bg-slate-50 px-4 py-2 text-xs dark:border-slate-800 dark:bg-slate-900">
                  <span className="font-semibold text-slate-400">Reached us on:</span>
                  <span className="font-medium text-slate-700 dark:text-slate-200">
                    {currentThread.number_label}
                  </span>
                </div>
              )}
              {(customer?.conversations?.length ?? 0) > 1 && (
                <div className="flex items-center gap-2 border-b border-slate-100 bg-slate-50 px-4 py-2 text-xs dark:border-slate-800 dark:bg-slate-900">
                  <span className="font-semibold text-slate-400">Reached us on:</span>
                  {customer?.conversations.map((c, i) => (
                    <button
                      key={c.id}
                      type="button"
                      onClick={() => setActiveThreadIndex(i)}
                      className={`rounded px-2 py-0.5 font-medium ${
                        activeThreadIndex === i
                          ? "bg-white text-slate-900 shadow-xs dark:bg-slate-800 dark:text-white"
                          : "text-slate-500"
                      }`}
                    >
                      {c.number_label} ({words(c.sales_stage)})
                    </button>
                  ))}
                </div>
              )}
              <ChatThread thread={currentThread} />
            </div>
          ) : activeTab === "attribution" ? (
            <div className="overflow-y-auto p-5 space-y-4">
              <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500">
                Generated Download Links ({customer?.links?.length ?? 0})
              </h3>
              {customer?.links && customer.links.length > 0 ? (
                <div className="space-y-3">
                  {customer.links.map((link) => (
                    <div
                      key={link.token}
                      className="rounded-xl border border-slate-200/80 bg-slate-50/50 p-4 dark:border-slate-800 dark:bg-slate-900/40 space-y-3"
                    >
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                          <span className="text-base">{link.platform === "macos" ? "🍎" : "🪟"}</span>
                          <span className="font-bold text-xs uppercase tracking-wider text-slate-700 dark:text-slate-300">
                            {link.platform ?? "Desktop"} Package
                          </span>
                        </div>
                        <div className="flex items-center gap-1.5">
                          {link.activated_at ? (
                            <Tag tone="emerald" dot>Activated</Tag>
                          ) : link.downloaded_at ? (
                            <Tag tone="teal" dot>Downloaded</Tag>
                          ) : link.clicked_at ? (
                            <Tag tone="indigo">Clicked</Tag>
                          ) : (
                            <Tag tone="neutral">Sent (Pending Click)</Tag>
                          )}
                        </div>
                      </div>

                      <div className="flex items-center justify-between rounded-lg bg-white p-2.5 font-mono text-xs border border-slate-200 dark:border-slate-800 dark:bg-slate-950">
                        <span className="truncate pr-2 text-slate-600 dark:text-slate-400">
                          {link.url}
                        </span>
                        <CopyButton text={link.url} label="Copy Link" />
                      </div>

                      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 text-xs">
                        <FactCard label="Sent" value={dateTime(link.sent_at) ?? <Dash />} />
                        <FactCard label="Clicked" value={dateTime(link.clicked_at) ?? <Dash />} />
                        <FactCard label="Downloaded" value={dateTime(link.downloaded_at) ?? <Dash />} />
                        <FactCard label="Activated" value={dateTime(link.activated_at) ?? <Dash />} />
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-slate-400 dark:text-slate-500 italic">
                  No install links generated for this customer yet.
                </p>
              )}
            </div>
          ) : (
            <div className="overflow-y-auto p-5 space-y-4">
              <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500">
                Contact & Acquisition Metadata
              </h3>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <FactCard label="Full Name" value={customer?.full_name ?? <Dash />} />
                <FactCard
                  label="Phone Number"
                  value={customer?.phone ? `+${customer.phone}` : <Dash />}
                  action={customer?.phone ? <CopyButton text={`+${customer.phone}`} /> : undefined}
                />
                <FactCard label="Email Address" value={customer?.email ?? <Dash />} />
                <FactCard label="Locale & Country" value={`${customer?.locale ?? "Unknown"} (${flagInfo.country})`} />
                <FactCard
                  label="Reached Us On"
                  value={
                    customer && customer.numbers.length > 0 ? (
                      <span className="flex flex-wrap gap-1">
                        {customer.numbers.map((label) => (
                          <Tag key={label} tone="neutral">
                            {label}
                          </Tag>
                        ))}
                      </span>
                    ) : (
                      <Dash />
                    )
                  }
                />
                <FactCard label="Lead Source" value={customer?.source ? words(customer.source) : <Dash />} />
                <FactCard label="Campaign Name" value={customer?.campaign_name ?? "Direct / Organic"} />
                <FactCard label="First Seen Timestamp" value={dateTime(customer?.created_at ?? null) ?? <Dash />} />
                <FactCard
                  label="Handling Mode"
                  value={currentThread?.handling_mode ? words(currentThread.handling_mode).toUpperCase() : "AI"}
                />
              </div>
            </div>
          )}
        </div>

        {/* Footer with clean close button and Esc hint */}
        <footer className="flex items-center justify-between border-t border-slate-100 bg-slate-50/70 px-5 py-3 dark:border-slate-800/80 dark:bg-slate-950/60">
          <div className="flex items-center gap-2 text-[11.5px] text-slate-400 dark:text-slate-500">
            <kbd className="rounded border border-slate-200 bg-white px-1.5 py-0.5 font-mono text-[10px] text-slate-500 shadow-2xs dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400">
              Esc
            </kbd>
            <span>to close</span>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3.5 py-1.5 text-xs font-semibold text-slate-700 shadow-2xs hover:bg-slate-50 hover:border-slate-300 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800 transition-all cursor-pointer"
          >
            Close
          </button>
        </footer>
      </div>
    </div>
  );
}
