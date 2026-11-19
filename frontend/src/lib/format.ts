export function fmtTime(ms: number, withTenths = false): string {
  const neg = ms < 0;
  const total = Math.max(0, Math.abs(ms));
  const s = Math.floor(total / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const tenths = withTenths ? `.${Math.floor((total % 1000) / 100)}` : "";
  const body = h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
  return `${neg ? "-" : ""}${body}${tenths}`;
}

export function fmtDuration(ms: number): string {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function fmtBytes(n: number | null | undefined): string {
  if (!n) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
export const fmtCompact = (n: number | null | undefined) => (n == null ? "—" : compact.format(n));
export const fmtMoney = (n: number | null | undefined) =>
  n == null ? "—" : n.toLocaleString("en", { style: "currency", currency: "USD", maximumFractionDigits: n < 100 ? 2 : 0 });

export function timeAgo(iso: string): string {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export const plural = (n: number, word: string) => `${fmtCompact(n)} ${word}${n === 1 ? "" : "s"}`;

export const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** Index of the last item whose start <= t (items sorted by start). -1 if none. */
export function lastStartingBefore<T>(items: T[], t: number, start: (item: T) => number): number {
  let lo = 0;
  let hi = items.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (start(items[mid]) <= t) {
      ans = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return ans;
}

export const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  downloading: "Downloading",
  processing: "Processing",
  transcribing: "Transcribing",
  analyzing: "Finding moments",
  ready: "Ready",
  failed: "Failed",
  running: "Rendering",
  done: "Done",
};

export function scoreTone(score: number | null | undefined): "hot" | "warm" | "cool" {
  if (score == null) return "cool";
  if (score >= 80) return "hot";
  if (score >= 62) return "warm";
  return "cool";
}
