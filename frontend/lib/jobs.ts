export const ACTIVE_STATUSES = new Set([
  "queued",
  "downloading",
  "transcribing",
  "scoring",
  "clipping",
]);

export function listRefetchInterval(statuses: readonly string[]): number | false {
  return statuses.some((status) => ACTIVE_STATUSES.has(status)) ? 2500 : false;
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: string): boolean {
  return UUID_RE.test(value);
}

export function downloadSlug(title: string | null | undefined): string {
  if (!title) {
    return "clip";
  }
  const slug = title
    .replace(/[^A-Za-z0-9_-]+/g, "-")
    .replace(/^[-_]+|[-_]+$/g, "")
    .slice(0, 60)
    .replace(/^[-_]+|[-_]+$/g, "");
  return slug || "clip";
}

export function truncateUrl(url: string, maxLen = 64): string {
  const cleaned = url.replace(/^https?:\/\//i, "");
  if (cleaned.length <= maxLen) {
    return cleaned;
  }
  return `${cleaned.slice(0, Math.max(1, maxLen - 1))}…`;
}

export function formatDuration(seconds: number | null | undefined): string | null {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) {
    return null;
  }
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function formatClipRange(startS: number, endS: number): string {
  const start = formatDuration(startS) ?? "0:00";
  const end = formatDuration(endS) ?? "0:00";
  return `${start} – ${end}`;
}

export function formatRelativeTime(
  isoString: string,
  nowMs: number = Date.now()
): string {
  const ts = Date.parse(isoString);
  if (Number.isNaN(ts)) {
    return isoString;
  }
  const diffSec = Math.max(0, Math.floor((nowMs - ts) / 1000));
  if (diffSec < 45) {
    return "just now";
  }
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) {
    return `${diffMin}m ago`;
  }
  const diffHours = Math.floor(diffMin / 60);
  if (diffHours < 24) {
    return `${diffHours}h ago`;
  }
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 30) {
    return `${diffDays}d ago`;
  }
  return new Date(ts).toISOString().slice(0, 10);
}

export function safeHttpUrl(raw: string): string | null {
  try {
    const parsed = new URL(raw);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return parsed.toString();
    }
    return null;
  } catch {
    return null;
  }
}
