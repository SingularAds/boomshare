import { useState } from "react";

export function TokenGate({
  message,
  onSubmit,
}: {
  message: string;
  onSubmit: (token: string, remember: boolean) => Promise<string | null>;
}) {
  const [value, setValue] = useState("");
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState(message);
  const [busy, setBusy] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!value.trim()) {
      setError("Please enter your admin token.");
      return;
    }
    setBusy(true);
    setError("");
    setError((await onSubmit(value.trim(), remember)) ?? "");
    setBusy(false);
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <form
          onSubmit={submit}
          className="relative overflow-hidden rounded-2xl border border-slate-200/90 bg-white p-7 shadow-xl dark:border-slate-800 dark:bg-[#111622]"
        >
          {/* Brand header */}
          <div className="flex items-center gap-3 mb-6">
            <div className="grid size-10 place-items-center rounded-xl bg-emerald-600 text-white shadow-sm">
              <svg className="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.2} d="M13 10V3L4 14h7v7l9-11h-7z" />
              </svg>
            </div>
            <div>
              <h2 className="text-base font-bold text-slate-900 dark:text-white">
                Boomshare
              </h2>
              <p className="text-xs text-slate-500 dark:text-slate-400 font-medium">
                Admin Dashboard
              </p>
            </div>
          </div>

          <div className="space-y-1 mb-5">
            <h1 className="text-lg font-bold text-slate-900 dark:text-white">
              Sign In
            </h1>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              Enter your admin token to access the dashboard.
            </p>
          </div>

          <div className="space-y-3.5">
            <div>
              <label className="block text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1.5">
                Admin Token
              </label>
              <div className="relative">
                <input
                  type={showPassword ? "text" : "password"}
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  placeholder="Paste admin token..."
                  autoComplete="off"
                  spellCheck={false}
                  autoFocus
                  className="w-full rounded-lg border border-slate-200 bg-slate-50/70 px-3.5 py-2.5 pr-10 font-mono text-xs outline-none transition-all focus:border-emerald-500 focus:bg-white focus:ring-2 focus:ring-emerald-500/20 dark:border-slate-700 dark:bg-slate-900/60 dark:text-white dark:focus:border-emerald-500 dark:focus:bg-slate-900"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 cursor-pointer"
                  tabIndex={-1}
                  aria-label={showPassword ? "Hide token" : "Show token"}
                >
                  {showPassword ? (
                    <svg className="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l18 18" />
                    </svg>
                  ) : (
                    <svg className="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                    </svg>
                  )}
                </button>
              </div>
            </div>

            {/* Remember Me */}
            <label
              htmlFor="remember-me"
              className="inline-flex cursor-pointer items-center gap-2.5 select-none group"
            >
              <div
                className={`size-4 rounded border transition-all flex items-center justify-center
                  ${remember
                    ? "border-emerald-500 bg-emerald-500"
                    : "border-slate-300 bg-white dark:border-slate-600 dark:bg-slate-800"
                  }`}
              >
                {remember && (
                  <svg className="size-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                )}
              </div>
              <input
                type="checkbox"
                id="remember-me"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
                className="sr-only"
              />
              <span className="text-xs text-slate-600 dark:text-slate-400 group-hover:text-slate-800 dark:group-hover:text-slate-200 transition-colors">
                Remember on this device
              </span>
            </label>

            <button
              type="submit"
              disabled={busy}
              className="w-full inline-flex items-center justify-center gap-2 rounded-lg bg-emerald-600 px-4 py-2.5 text-xs font-bold text-white shadow-sm hover:bg-emerald-500 active:scale-[0.99] disabled:opacity-50 disabled:cursor-not-allowed transition-all cursor-pointer dark:bg-emerald-500 dark:text-slate-950 dark:hover:bg-emerald-400"
            >
              {busy ? (
                <>
                  <svg className="size-4 animate-spin" viewBox="0 0 24 24" fill="none">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                  </svg>
                  <span>Signing in…</span>
                </>
              ) : (
                <span>Sign In</span>
              )}
            </button>
          </div>

          {error && (
            <div className="mt-4 rounded-lg bg-rose-50 p-2.5 text-center text-xs font-semibold text-rose-700 dark:bg-rose-950/40 dark:text-rose-300 border border-rose-200 dark:border-rose-900/50">
              {error}
            </div>
          )}

          <div className="mt-5 pt-4 border-t border-slate-100 text-center dark:border-slate-800">
            <span className="text-[11px] text-slate-400 dark:text-slate-500">
              Authorized access only
            </span>
          </div>
        </form>
      </div>
    </div>
  );
}
