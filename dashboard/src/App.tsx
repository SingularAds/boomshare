import { useCallback, useEffect, useRef, useState } from "react";
import {
  Unauthorised,
  clearToken,
  fetchCustomer,
  fetchCustomers,
  fetchOverview,
  readToken,
  storeToken,
} from "./api";
import type { CustomerDetail, CustomerPage, Outcome, Overview } from "./types";
import { CustomerModal } from "./components/CustomerModal";
import { CustomerTable } from "./components/CustomerTable";
import { KpiRow, KpiSkeleton, Panels, PanelsSkeleton } from "./components/Overview";
import { TokenGate } from "./components/TokenGate";

const PAGE_SIZE = 50;
const SEARCH_DEBOUNCE_MS = 300;

export default function App() {
  const [token, setToken] = useState(readToken);
  const [gateMessage, setGateMessage] = useState("");

  const [overview, setOverview] = useState<Overview | null>(null);
  const [page, setPage] = useState<CustomerPage | null>(null);
  const [tableLoading, setTableLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [outcome, setOutcome] = useState<Outcome>("");
  const [offset, setOffset] = useState(0);
  // Real customers only — the toggle has been removed; this is always true.
  const realOnly = true;

  const [selected, setSelected] = useState<CustomerDetail | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [modalLoading, setModalLoading] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  const tableRef = useRef<HTMLDivElement>(null);

  const signOut = useCallback((message: string) => {
    clearToken();
    setToken("");
    setOverview(null);
    setPage(null);
    setGateMessage(message);
  }, []);

  const loadAll = useCallback(
    async (signal?: AbortSignal) => {
      if (!token) return;
      setTableLoading(true);
      setRefreshing(true);
      setFailure(null);
      try {
        const [nextOverview, nextPage] = await Promise.all([
          fetchOverview(token, !realOnly, signal),
          fetchCustomers(
            token,
            { limit: PAGE_SIZE, offset: 0, search, outcome, includeTest: !realOnly },
            signal,
          ),
        ]);
        setOverview(nextOverview);
        setPage(nextPage);
        setOffset(0);
      } catch (error) {
        if (signal?.aborted) return;
        if (error instanceof Unauthorised) signOut(error.message);
        else setFailure(error instanceof Error ? error.message : String(error));
      } finally {
        if (!signal?.aborted) {
          setTableLoading(false);
          setRefreshing(false);
        }
      }
    },
    [token, search, outcome, realOnly, signOut],
  );

  const firstRun = useRef(true);
  useEffect(() => {
    if (!token) return;
    if (firstRun.current) {
      firstRun.current = false;
      const controller = new AbortController();
      void loadAll(controller.signal);
      return () => controller.abort();
    }
  }, [token, loadAll]);

  useEffect(() => {
    if (!token || firstRun.current) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setTableLoading(true);
      try {
        setPage(
          await fetchCustomers(
            token,
            { limit: PAGE_SIZE, offset, search, outcome, includeTest: !realOnly },
            controller.signal,
          ),
        );
        setFailure(null);
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof Unauthorised) signOut(error.message);
        else setFailure(error instanceof Error ? error.message : String(error));
      } finally {
        if (!controller.signal.aborted) setTableLoading(false);
      }
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [token, offset, search, outcome, realOnly, signOut]);

  async function openCustomer(id: string) {
    setSelectedId(id);
    setSelected(null);
    setModalError(null);
    setModalLoading(true);
    try {
      setSelected(await fetchCustomer(token, id));
    } catch (error) {
      if (error instanceof Unauthorised) {
        setSelectedId(null);
        signOut(error.message);
        return;
      }
      setModalError(error instanceof Error ? error.message : String(error));
    } finally {
      setModalLoading(false);
    }
  }

  function handleFilterFromKpi(newOutcome: Outcome) {
    setOutcome(newOutcome);
    setOffset(0);
    // Smooth scroll down to table
    tableRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function authenticate(candidate: string, remember: boolean): Promise<string | null> {
    try {
      await fetchOverview(candidate, false);
      storeToken(candidate, remember);
      firstRun.current = true;
      setToken(candidate);
      setGateMessage("");
      return null;
    } catch (error) {
      if (error instanceof Unauthorised) return error.message;
      return `Connection failed: ${error instanceof Error ? error.message : String(error)}`;
    }
  }

  if (!token) return <TokenGate message={gateMessage} onSubmit={authenticate} />;

  return (
    <div className="min-h-screen pb-16">
      {/* Executive Header */}
      <header className="sticky top-0 z-30 border-b border-slate-200/80 bg-white/80 shadow-[0_1px_3px_rgba(0,0,0,0.02)] backdrop-blur-xl dark:border-slate-800/80 dark:bg-[#0b0f17]/85">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-4 py-3 sm:px-6">
          {/* Brand & Live status beacon */}
          <div className="flex items-center gap-3">
            <div className="grid size-9 place-items-center rounded-xl bg-gradient-to-tr from-emerald-600 to-teal-400 text-white shadow-sm">
              <svg className="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.2} d="M13 10V3L4 14h7v7l9-11h-7z" />
              </svg>
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-sm font-extrabold tracking-tight text-slate-900 dark:text-white">
                  Boomshare
                </span>
                <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[10px] font-bold text-emerald-700 dark:text-emerald-400 border border-emerald-500/20">
                  <span className="size-1.5 rounded-full bg-emerald-500 pulse-beacon" />
                  <span>PIPELINE ACTIVE</span>
                </span>
              </div>
              <p className="text-[11px] text-slate-400 dark:text-slate-500 font-medium">
                {overview
                  ? `Synced at ${new Date(overview.generated_at).toLocaleTimeString()}`
                  : "Connecting..."}
              </p>
            </div>
          </div>

          {/* Executive Header Controls */}
          <div className="flex items-center gap-2">
            {/* Ticked, the page describes people from outside the team. Unticked,
                it also counts the numbers we test the pipeline with. */}
            {/* <label
              className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-slate-200/90 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 select-none hover:border-slate-300 dark:border-slate-700/80 dark:bg-[#111622] dark:text-slate-200"
              title="Our own test numbers are hidden unless you untick this."
            >
              <input
                type="checkbox"
                checked={realOnly}
                onChange={(e) => {
                  setRealOnly(e.target.checked);
                  setOffset(0);
                }}
                className="size-3.5 accent-emerald-600"
              />
              Real customers only
            </label> */}
            <button
              type="button"
              onClick={() => void loadAll()}
              disabled={tableLoading || refreshing}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200/90 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 shadow-2xs hover:bg-slate-50 hover:border-slate-300 dark:border-slate-700/80 dark:bg-[#111622] dark:text-slate-200 dark:hover:bg-slate-800/80 transition-all cursor-pointer disabled:opacity-50"
            >
              <svg
                className={`size-3.5 text-emerald-600 dark:text-emerald-400 ${refreshing ? "animate-spin" : ""}`}
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
              <span>{refreshing ? "Refreshing..." : "Refresh"}</span>
            </button>

            <button
              type="button"
              onClick={() => signOut("")}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200/90 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 shadow-2xs hover:border-rose-200 hover:bg-rose-50/60 hover:text-rose-600 dark:border-slate-700/80 dark:bg-[#111622] dark:text-slate-300 dark:hover:border-rose-900/50 dark:hover:bg-rose-950/30 dark:hover:text-rose-400 transition-all cursor-pointer"
            >
              <svg className="size-3.5 opacity-70" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
              </svg>
              <span>Sign Out</span>
            </button>
          </div>
        </div>
      </header>

      {/* Main Content Dashboard */}
      <main className="mx-auto max-w-7xl space-y-5 px-4 pt-6 sm:px-6">
        {failure && (
          <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50/90 p-4 text-xs font-semibold text-rose-700 shadow-xs dark:border-rose-900/60 dark:bg-rose-950/40 dark:text-rose-300">
            <span>{failure}</span>
            <button
              type="button"
              onClick={() => setFailure(null)}
              className="text-rose-500 hover:text-rose-700 font-bold"
            >
              ✕
            </button>
          </div>
        )}

        {/* A number Meta routes to us that this deployment does not answer on.
            Those customers messaged and got silence, so it leads the page. */}
        {overview && overview.unanswerable_by_number.length > 0 && (
          <div className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 shadow-xs dark:border-amber-900/60 dark:bg-amber-950/50 dark:text-amber-200">
            <p className="font-bold">
              Messages arrived on a WhatsApp number this deployment does not answer on
            </p>
            <p className="mt-1 text-[13px]">
              They were stored but never replied to. Add the number to{" "}
              <code className="rounded bg-amber-100 px-1 dark:bg-amber-900/60">
                WHATSAPP_PHONE_NUMBER_IDS
              </code>{" "}
              if it is ours, then redeploy.
            </p>
            <ul className="mt-2 space-y-0.5 font-mono text-[12.5px]">
              {overview.unanswerable_by_number.map((row) => (
                <li key={row.key}>
                  {row.key} — {row.count} {row.count === 1 ? "thread" : "threads"}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Section 1: Executive KPI Cards */}
        {overview ? (
          <KpiRow
            data={overview}
            selectedOutcome={outcome}
            onSelectOutcome={handleFilterFromKpi}
          />
        ) : (
          <KpiSkeleton />
        )}

        {/* Section 2: Interactive Funnel & Attribution Panels */}
        {/* One skeleton row, because there is one row of panels - two would
            collapse to one the moment data arrived. */}
        {overview ? (
          <Panels data={overview} onSelectOutcome={handleFilterFromKpi} />
        ) : (
          <PanelsSkeleton />
        )}

        {/* Section 3: Interactive Customer Table */}
        <div ref={tableRef} className="pt-2">
          {/* Active Filter Bar if filtered */}
          {(outcome || search) && (
            <div className="mb-3 flex items-center justify-between rounded-lg border border-emerald-500/20 bg-emerald-500/5 px-3.5 py-2 text-xs text-emerald-800 dark:text-emerald-300">
              <div className="flex items-center gap-2">
                <span className="font-bold">Active filter:</span>
                {outcome && (
                  <span className="rounded bg-emerald-500/10 px-2 py-0.5 font-semibold">
                    Outcome: {outcome}
                  </span>
                )}
                {search && (
                  <span className="rounded bg-emerald-500/10 px-2 py-0.5 font-semibold">
                    Search: "{search}"
                  </span>
                )}
              </div>
              <button
                type="button"
                onClick={() => {
                  setOutcome("");
                  setSearch("");
                }}
                className="font-bold hover:underline cursor-pointer"
              >
                Clear all filters
              </button>
            </div>
          )}

          <CustomerTable
            page={page}
            loading={tableLoading}
            search={search}
            outcome={outcome}
            onSearch={(value) => {
              setSearch(value);
              setOffset(0);
            }}
            onOutcome={(value) => {
              setOutcome(value);
              setOffset(0);
            }}
            onOpen={(id) => void openCustomer(id)}
            onPage={setOffset}
          />
        </div>
      </main>

      {/* Customer Conversation & Detail Modal */}
      {selectedId && (
        <CustomerModal
          customer={selected}
          loading={modalLoading}
          error={modalError}
          onClose={() => {
            setSelectedId(null);
            setSelected(null);
          }}
        />
      )}
    </div>
  );
}
