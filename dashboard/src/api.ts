import type { CustomerDetail, CustomerPage, Outcome, Overview } from "./types";

/**
 * The token is kept in one of two stores:
 *   - sessionStorage (default) — cleared when the tab closes.
 *   - localStorage  (remember) — survives browser restarts.
 *
 * Every request goes to the same origin that served this page, so the token
 * never crosses origins regardless of which store holds it.
 */
const KEY = "boomshare.admin.token";

export const readToken = (): string =>
  localStorage.getItem(KEY) ?? sessionStorage.getItem(KEY) ?? "";

export function storeToken(token: string, remember: boolean): void {
  if (remember) {
    localStorage.setItem(KEY, token);
    sessionStorage.removeItem(KEY);
  } else {
    sessionStorage.setItem(KEY, token);
    localStorage.removeItem(KEY);
  }
}

export function clearToken(): void {
  sessionStorage.removeItem(KEY);
  localStorage.removeItem(KEY);
}

/** Thrown on 401 so the app can drop back to the gate in one place. */
export class Unauthorised extends Error {
  constructor() {
    super("The admin token was rejected.");
    this.name = "Unauthorised";
  }
}

async function get<T>(path: string, token: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { headers: { "X-Admin-Token": token }, signal });
  if (response.status === 401) throw new Unauthorised();
  if (!response.ok) throw new Error(`Request failed (HTTP ${response.status}).`);
  return (await response.json()) as T;
}

export const fetchOverview = (token: string, includeTest: boolean, signal?: AbortSignal) =>
  get<Overview>(
    `/admin/dashboard/overview${includeTest ? "?include_test=true" : ""}`,
    token,
    signal,
  );

export function fetchCustomers(
  token: string,
  options: {
    limit: number;
    offset: number;
    search: string;
    outcome: Outcome;
    includeTest: boolean;
  },
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    limit: String(options.limit),
    offset: String(options.offset),
  });
  if (options.search.trim()) params.set("search", options.search.trim());
  if (options.outcome) params.set("outcome", options.outcome);
  if (options.includeTest) params.set("include_test", "true");
  return get<CustomerPage>(`/admin/dashboard/customers?${params}`, token, signal);
}

export const fetchCustomer = (token: string, id: string, signal?: AbortSignal) =>
  get<CustomerDetail>(`/admin/dashboard/customers/${encodeURIComponent(id)}`, token, signal);
