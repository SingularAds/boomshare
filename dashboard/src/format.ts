export const num = (value: number | null | undefined) => (value ?? 0).toLocaleString();

/** A share, rounded to one decimal. Zero denominator is 0, never NaN or "—". */
export const pct = (part: number, whole: number) =>
  whole > 0 ? Math.round((part / whole) * 1000) / 10 : 0;

/** Enum values arrive as `download_suggested`; people read "download suggested". */
export const words = (value: string) => value.replace(/_/g, " ");

export function ago(iso: string | null): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  const seconds = (Date.now() - then) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 2592000) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

export const day = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

export const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });

export const dateTime = (iso: string | null) => (iso ? `${day(iso)}, ${clock(iso)}` : null);

/** Detect country flag from E.164 phone digits */
export function countryFlag(phone: string): { flag: string; country: string } {
  const p = phone.replace(/^\+/, "");
  if (p.startsWith("55")) return { flag: "🇧🇷", country: "Brazil" };
  if (p.startsWith("1")) return { flag: "🇺🇸", country: "USA / CAN" };
  if (p.startsWith("34")) return { flag: "🇪🇸", country: "Spain" };
  if (p.startsWith("44")) return { flag: "🇬🇧", country: "UK" };
  if (p.startsWith("49")) return { flag: "🇩🇪", country: "Germany" };
  if (p.startsWith("33")) return { flag: "🇫🇷", country: "France" };
  if (p.startsWith("52")) return { flag: "🇲🇽", country: "Mexico" };
  if (p.startsWith("91")) return { flag: "🇮🇳", country: "India" };
  return { flag: "🌐", country: "Global" };
}

/** Colour tone for a sales stage */
export function stageTone(stage: string | null): "emerald" | "teal" | "amber" | "rose" | "indigo" | "neutral" {
  switch (stage) {
    case "activated":
      return "emerald";
    case "downloaded":
      return "teal";
    case "link_sent":
    case "download_suggested":
    case "product_explained":
    case "qualified":
      return "indigo";
    case "engaged":
    case "contacted":
    case "new":
      return "neutral";
    case "objection_handling":
    case "human_handoff":
      return "amber";
    case "not_interested":
    case "closed":
      return "rose";
    default:
      return "neutral";
  }
}
