/**
 * The one place values become display text.
 *
 * - Numbers, dates and times use the browser's locale via Intl.
 * - Anything missing renders as EMPTY.
 * - Ranges use one separator (RANGE_SEP), lists use LIST_SEP.
 */

export const EMPTY = '—';
export const RANGE_SEP = ' – ';
export const LIST_SEP = ' · ';

const METERS_TO_FEET = 3.28084;
const KMH_TO_KNOTS = 0.539957;

let locale: string | undefined;
const numberFormats = new Map<number, Intl.NumberFormat>();
const dateFormats = new Map<string, Intl.DateTimeFormat>();

/** Pin the locale (tests); `undefined` means the browser default. */
export function setFormatLocale(next: string | undefined): void {
  locale = next;
  numberFormats.clear();
  dateFormats.clear();
}

function toFinite(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

export function isFiniteNumber(value: unknown): boolean {
  return toFinite(value) != null;
}

/** Locale-grouped number ("12,345"), `digits` fraction digits. */
export function formatNumber(value: unknown, digits = 0): string {
  const n = toFinite(value);
  if (n == null) return EMPTY;
  let fmt = numberFormats.get(digits);
  if (!fmt) {
    fmt = new Intl.NumberFormat(locale, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
    numberFormats.set(digits, fmt);
  }
  return fmt.format(n);
}

// ── Kinematics ───────────────────────────────────────────────

/** "1,234 m (4,049 ft)" */
export function formatAltitude(meters: unknown): string {
  const m = toFinite(meters);
  if (m == null) return EMPTY;
  return `${formatNumber(m)} m (${formatNumber(m * METERS_TO_FEET)} ft)`;
}

/** "1,234 m" for tables, list rows, and tooltips. */
export function formatAltitudeCompact(meters: unknown): string {
  const m = toFinite(meters);
  return m == null ? EMPTY : `${formatNumber(m)} m`;
}

/** "320 km/h (173 kt)" */
export function formatSpeed(kmh: unknown): string {
  const v = toFinite(kmh);
  if (v == null) return EMPTY;
  return `${formatNumber(v)} km/h (${formatNumber(v * KMH_TO_KNOTS)} kt)`;
}

/** "320 km/h" */
export function formatSpeedCompact(kmh: unknown): string {
  const v = toFinite(kmh);
  return v == null ? EMPTY : `${formatNumber(v)} km/h`;
}

export function formatHeading(degrees: unknown): string {
  const d = toFinite(degrees);
  return d == null ? EMPTY : `${formatNumber(d)}°`;
}

/** Latitude or longitude, always 5 decimals (~1 m). */
export function formatCoordinate(value: unknown): string {
  return formatNumber(value, 5);
}

export function formatPosition(lat: unknown, lon: unknown): string {
  if (toFinite(lat) == null || toFinite(lon) == null) return EMPTY;
  return `${formatCoordinate(lat)}, ${formatCoordinate(lon)}`;
}

// ── Durations ────────────────────────────────────────────────

/** "45s", "3m 20s", "2h 5m". Zero/negative is "0s"; missing is EMPTY. */
export function formatDuration(seconds: unknown): string {
  const s = toFinite(seconds);
  if (s == null) return EMPTY;
  const total = Math.max(0, Math.round(s));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  if (minutes > 0) return secs > 0 ? `${minutes}m ${secs}s` : `${minutes}m`;
  return `${secs}s`;
}

/** Time to reach a zone, same style as durations. */
export function formatEta(eta: unknown): string {
  return formatDuration(eta);
}

export function formatEpisodeDuration(
  activatedAt?: number | null,
  deactivatedAt?: number | null,
  now = Date.now() / 1000,
): string {
  if (!activatedAt) return EMPTY;
  const end = deactivatedAt ?? now;
  return formatDuration(Math.max(0, end - activatedAt));
}

/** "12s ago", "3m 5s ago" (never negative). */
export function formatAgo(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts || !Number.isFinite(ts)) return EMPTY;
  return `${formatDuration(Math.max(0, now - ts))} ago`;
}

// ── Dates & times ────────────────────────────────────────────

function dateFormat(key: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  let fmt = dateFormats.get(key);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat(locale, options);
    dateFormats.set(key, fmt);
  }
  return fmt;
}

const DATE_OPTS: Intl.DateTimeFormatOptions = { year: 'numeric', month: 'short', day: 'numeric' };
const TIME_OPTS: Intl.DateTimeFormatOptions = { hour: 'numeric', minute: '2-digit' };

function validTs(ts: number | null | undefined): ts is number {
  return !!ts && Number.isFinite(ts);
}

export function formatDate(ts?: number | null): string {
  if (!validTs(ts)) return EMPTY;
  return dateFormat('date', DATE_OPTS).format(ts * 1000);
}

export function formatTime(ts?: number | null, opts?: { withSeconds?: boolean }): string {
  if (!validTs(ts)) return EMPTY;
  const withSeconds = !!opts?.withSeconds;
  return dateFormat(withSeconds ? 'time-s' : 'time', {
    ...TIME_OPTS,
    ...(withSeconds ? { second: '2-digit' } : {}),
  }).format(ts * 1000);
}

export function formatDateTime(ts?: number | null, opts?: { withSeconds?: boolean }): string {
  if (!validTs(ts)) return EMPTY;
  const withSeconds = !!opts?.withSeconds;
  return dateFormat(withSeconds ? 'datetime-s' : 'datetime', {
    ...DATE_OPTS,
    ...TIME_OPTS,
    ...(withSeconds ? { second: '2-digit' } : {}),
  }).format(ts * 1000);
}

export function isSameLocalDay(a?: number | null, b?: number | null): boolean {
  if (!a || !b) return false;
  const da = new Date(a * 1000);
  const db = new Date(b * 1000);
  return (
    da.getFullYear() === db.getFullYear() &&
    da.getMonth() === db.getMonth() &&
    da.getDate() === db.getDate()
  );
}

/**
 * "Nov 14, 2023, 10:13 PM – 11:13 PM" on one day,
 * "Nov 14, 2023, 10:13 PM – Nov 15, 2023, 1:02 AM" across days,
 * or just the start when there is no end.
 */
export function formatTimeRange(
  start?: number | null,
  end?: number | null,
  opts?: { withSeconds?: boolean },
): string {
  if (!validTs(start)) return validTs(end) ? formatDateTime(end, opts) : EMPTY;
  if (!validTs(end) || end === start) return formatDateTime(start, opts);
  const tail = isSameLocalDay(start, end) ? formatTime(end, opts) : formatDateTime(end, opts);
  return `${formatDateTime(start, opts)}${RANGE_SEP}${tail}`;
}

// ── Identity fields ──────────────────────────────────────────

const PLACEHOLDERS = new Set(['', 'unknown', 'unknown model', 'unknown owner', 'n/a', 'none', 'null']);

/** Trimmed value, or null for missing/placeholder text from older servers. */
export function cleanField(value: string | null | undefined): string | null {
  if (value == null) return null;
  const trimmed = String(value).trim();
  return PLACEHOLDERS.has(trimmed.toLowerCase()) ? null : trimmed;
}

export function displayField(value: string | null | undefined): string {
  return cleanField(value) ?? EMPTY;
}

export function displayIcao(icao: string | null | undefined): string {
  return cleanField(icao)?.toUpperCase() ?? EMPTY;
}

/** Callsign, else ICAO, else EMPTY. */
export function displayCallsign(source: {
  callsign?: string | null;
  icao?: string | null;
}): string {
  return cleanField(source.callsign) ?? displayIcao(source.icao);
}

/** "Zone · rule" */
export function formatZoneRule(zone?: string | null, rule?: string | null): string {
  const zoneName = cleanField(zone) ?? 'Zone';
  const ruleName = cleanField(rule);
  return ruleName ? `${zoneName}${LIST_SEP}${ruleName}` : zoneName;
}

export function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${formatNumber(count)} ${count === 1 ? singular : plural}`;
}

/** Only https: URLs are allowed for externally hosted links/images. */
export function safeHttpsUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' ? parsed.href : null;
  } catch {
    return null;
  }
}

export function isFlightLive(flight: { is_live?: boolean }): boolean {
  return !!flight.is_live;
}
