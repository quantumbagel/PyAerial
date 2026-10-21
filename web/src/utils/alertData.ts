/**
 * Alert domain: episode identity/state/merging, severity, colours, sorting,
 * and the alert summaries shown in lists and the drawer.
 */
import type { ActiveAlert, Alert, AlertStats, Zone } from '../api/types';
import { shade } from './colors';
import {
  EMPTY,
  LIST_SEP,
  RANGE_SEP,
  cleanField,
  displayCallsign,
  displayIcao,
  formatAltitude,
  formatAltitudeCompact,
  formatDateTime,
  formatDuration,
  formatEpisodeDuration,
  formatEta,
  formatPosition,
  formatTime,
  formatZoneRule,
  isSameLocalDay,
  pluralize,
} from './format';
import { readStoredJson, writeStored } from './storage';
import { alertColorFor, getAccessibleBadgeTextColor } from './zoneColors';

// ── Episode identity & state ─────────────────────────────────

type EpisodeLike = {
  alert_id?: string | null;
  flight_id?: string | null;
  icao?: string | null;
  callsign?: string | null;
  zone?: string | null;
  rule?: string | null;
  active?: boolean;
  activated_at?: number | null;
  deactivated_at?: number | null;
};

/** Stable identity for an alert episode (unchanged when it goes from active to ended). */
export function alertEpisodeIdentity(alert: EpisodeLike): string {
  if (alert.alert_id) return alert.alert_id;
  if (alert.flight_id && alert.zone && alert.rule) {
    return `${alert.flight_id}:${alert.zone}:${alert.rule}`;
  }
  if (alert.activated_at != null) return `episode:${Math.round(alert.activated_at)}`;
  return `episode:${alert.icao || alert.callsign || ''}:${alert.zone || ''}:${alert.rule || ''}`;
}

/** Row key for an alert episode. */
export function alertEpisodeKey(alert: EpisodeLike): string {
  return alertEpisodeIdentity(alert);
}

/** The one definition of "this episode is still active". */
export function isAlertActive(alert: { active?: boolean; deactivated_at?: number | null }): boolean {
  return alert.active !== false && !(alert.deactivated_at != null && alert.deactivated_at > 0);
}

/**
 * Merge two snapshots of the same episode. Later fields win, but "ended" is
 * terminal: once either side has ended, the result has ended.
 */
export function mergeEpisode<T extends EpisodeLike>(existing: T, update: T): T {
  const deactivated_at = update.deactivated_at ?? existing.deactivated_at ?? null;
  const ended = !isAlertActive(existing) || !isAlertActive(update) || deactivated_at != null;
  const starts = [existing.activated_at, update.activated_at].filter(
    (t): t is number => typeof t === 'number' && t > 0,
  );
  return {
    ...existing,
    ...update,
    alert_id: update.alert_id || existing.alert_id,
    zone: update.zone || existing.zone,
    rule: update.rule || existing.rule,
    activated_at: starts.length ? Math.min(...starts) : (update.activated_at ?? existing.activated_at),
    deactivated_at: deactivated_at ?? undefined,
    active: !ended,
  };
}

function shallowEqual(a: object, b: object): boolean {
  const ra = a as Record<string, unknown>;
  const rb = b as Record<string, unknown>;
  const keys = new Set([...Object.keys(ra), ...Object.keys(rb)]);
  for (const key of keys) {
    const va = ra[key];
    const vb = rb[key];
    if (va === vb || (va == null && vb == null)) continue;
    if (Array.isArray(va) && Array.isArray(vb) && va.length === 0 && vb.length === 0) continue;
    return false;
  }
  return true;
}

/** Collapse duplicate snapshots of the same episode into one entry. */
export function dedupeAlerts<T extends EpisodeLike>(alerts: T[]): T[] {
  if (alerts.length < 2) return alerts;
  const map = new Map<string, T>();
  for (const alert of alerts) {
    const key = alertEpisodeIdentity(alert);
    const existing = map.get(key);
    map.set(key, existing ? mergeEpisode(existing, alert) : alert);
  }
  return map.size === alerts.length ? alerts : Array.from(map.values());
}

/**
 * Fold `updates` into `existing` by episode. Existing order is kept, new
 * episodes are appended, and unchanged entries keep their identity.
 */
export function mergeAlertsByEpisode(existing: Alert[], updates: Alert[]): Alert[] {
  if (updates.length === 0) return dedupeAlerts(existing);
  const updateMap = new Map<string, Alert>();
  for (const alert of updates) {
    const key = alertEpisodeIdentity(alert);
    const prior = updateMap.get(key);
    updateMap.set(key, prior ? mergeEpisode(prior, alert) : alert);
  }
  const seen = new Set<string>();
  const merged: Alert[] = [];
  for (const alert of dedupeAlerts(existing)) {
    const key = alertEpisodeIdentity(alert);
    seen.add(key);
    const update = updateMap.get(key);
    if (!update) {
      merged.push(alert);
      continue;
    }
    const next = mergeEpisode(alert, update);
    merged.push(shallowEqual(alert, next) ? alert : next);
  }
  for (const [key, alert] of updateMap) {
    if (!seen.has(key)) merged.push(alert);
  }
  return merged;
}

// ── Severity & colour ────────────────────────────────────────

export type AlertSeverity = 'alert' | 'warn' | 'info';

const SEVERITY_RANK: Record<AlertSeverity, number> = { alert: 2, warn: 1, info: 0 };

/** Severity inferred from a rule name; only used to rank concurrent alerts. */
export function alertSeverity(rule?: string | null): AlertSeverity {
  const str = (rule || '').toLowerCase().trim();
  if (['alert', 'critical', 'danger', 'high', 'error'].includes(str)) return 'alert';
  if (['warn', 'warning', 'medium', 'caution'].includes(str)) return 'warn';
  return 'info';
}

export function alertSeverityRank(rule?: string | null): number {
  return SEVERITY_RANK[alertSeverity(rule)];
}

/** The alert that should represent a group (highest severity, then first). */
export function primaryAlert<T extends { rule?: string | null }>(alerts?: T[] | null): T | null {
  if (!alerts?.length) return null;
  let best = alerts[0];
  for (const alert of alerts) {
    if (alertSeverityRank(alert.rule) > alertSeverityRank(best.rule)) best = alert;
  }
  return best;
}

export interface AlertVisual {
  /** The configured zone/rule colour. */
  color: string;
  /** A darker variant for outlines. */
  dark: string;
  /** Readable text colour on dark panels. */
  text: string;
  severity: AlertSeverity;
  label: string;
}

/**
 * How an alert looks everywhere (plane icon, path overlay, timeline edge,
 * badges): always the configured zone/rule colour.
 */
export function alertVisual(
  alert: { zone?: string | null; rule?: string | null },
  zones?: Zone[],
  alertColors?: Record<string, string>,
): AlertVisual {
  const zone = cleanField(alert.zone) ?? 'zone';
  const { stroke } = alertColorFor(zone, alert.rule, zones, alertColors);
  return {
    color: stroke,
    dark: shade(stroke, 0.55),
    text: getAccessibleBadgeTextColor(stroke),
    severity: alertSeverity(alert.rule),
    label: formatZoneRule(alert.zone, alert.rule),
  };
}

// ── Summaries ────────────────────────────────────────────────

/** "Zone · rule (3m 5s), Zone2 · rule" */
export function formatActiveAlerts(
  alerts?: { zone?: string | null; rule?: string | null; activated_at?: number | null }[] | null,
  now = Date.now() / 1000,
): string {
  if (!alerts?.length) return 'None';
  return alerts
    .map((a) => {
      const duration = a.activated_at ? ` (${formatEpisodeDuration(a.activated_at, null, now)})` : '';
      return `${formatZoneRule(a.zone, a.rule)}${duration}`;
    })
    .join(', ');
}

/** "2 episodes · 5m alerted" or "No alerts". */
export function formatAlertHistory(stats?: Partial<AlertStats> | null): string {
  const count = stats?.episode_count ?? 0;
  if (count <= 0) return 'No alerts';
  return `${pluralize(count, 'episode')}${LIST_SEP}${formatDuration(stats?.total_seconds ?? 0)} alerted`;
}

/** Short alert summary for a flight row: active alerts first, else history. */
export function flightAlertLabel(flight: {
  active_alerts?: ActiveAlert[] | null;
  alert_stats?: Partial<AlertStats> | null;
}): string {
  const active = flight.active_alerts?.length ?? 0;
  if (active > 0) return `${active} active`;
  return formatAlertHistory(flight.alert_stats);
}

/** "Nov 14, 10:13 PM – 10:20 PM (7m)" with the episode duration. */
export function formatEpisodeSpan(alert: EpisodeLike, now = Date.now() / 1000): string {
  const start = alert.activated_at;
  if (!start) return EMPTY;
  const duration = formatEpisodeDuration(start, alert.deactivated_at, now);
  const end = isAlertActive(alert) ? null : alert.deactivated_at;
  if (!end) return `${formatDateTime(start, { withSeconds: true })} (${duration})`;
  const tail = isSameLocalDay(start, end)
    ? formatTime(end, { withSeconds: true })
    : formatDateTime(end, { withSeconds: true });
  return `${formatDateTime(start, { withSeconds: true })}${RANGE_SEP}${tail} (${duration})`;
}

/** Multi-line hover text for an alert episode. */
export function alertTooltip(alert: Alert, now = Date.now() / 1000): string {
  const active = isAlertActive(alert);
  const lines = [
    `${active ? 'Active' : 'Ended'} episode${LIST_SEP}${formatEpisodeDuration(alert.activated_at, alert.deactivated_at, now)}`,
    `Activated: ${formatDateTime(alert.activated_at, { withSeconds: true })}`,
  ];
  if (!active && alert.deactivated_at) {
    lines.push(`Ended: ${formatDateTime(alert.deactivated_at, { withSeconds: true })}`);
  }
  lines.push(`Position: ${formatPosition(alert.latitude, alert.longitude)}`);
  lines.push(`Altitude: ${formatAltitude(alert.altitude)}`);
  if (active) lines.push(`ETA: ${formatEta(alert.eta)}`);
  return lines.join('\n');
}

// ── Sorting ──────────────────────────────────────────────────

export type AlertSortField = 'activated' | 'duration' | 'status' | 'zone' | 'callsign' | 'icao' | 'altitude';

export type SortDirection = 'asc' | 'desc';

export const ALERT_SORT_OPTIONS: { value: AlertSortField; label: string }[] = [
  { value: 'activated', label: 'Activated' },
  { value: 'duration', label: 'Duration' },
  { value: 'status', label: 'Status' },
  { value: 'zone', label: 'Zone' },
  { value: 'callsign', label: 'Callsign' },
  { value: 'icao', label: 'ICAO' },
  { value: 'altitude', label: 'Altitude' },
];

/** Fields that existed before and now map to a remaining option. */
const LEGACY_ALERT_SORT: Record<string, AlertSortField> = { active: 'status', rule: 'zone' };

export function defaultAlertSortDirection(field: AlertSortField): SortDirection {
  return field === 'zone' || field === 'callsign' || field === 'icao' ? 'asc' : 'desc';
}

function alertDuration(alert: Alert, now = Date.now() / 1000): number {
  if (!alert.activated_at) return 0;
  const end = alert.deactivated_at ?? now;
  return Math.max(0, end - alert.activated_at);
}

const textCompare = (a?: string | null, b?: string | null) =>
  (a || '').localeCompare(b || '', undefined, { sensitivity: 'base' });

function compareAlertsByField(a: Alert, b: Alert, field: AlertSortField): number {
  switch (field) {
    case 'activated':
      return (a.activated_at ?? 0) - (b.activated_at ?? 0);
    case 'duration':
      return alertDuration(a) - alertDuration(b);
    case 'status':
      return Number(isAlertActive(a)) - Number(isAlertActive(b));
    case 'zone':
      return textCompare(a.zone, b.zone) || textCompare(a.rule, b.rule);
    case 'callsign':
      return textCompare(displayCallsign(a), displayCallsign(b));
    case 'icao':
      return textCompare(a.icao, b.icao);
    case 'altitude':
      return (a.altitude ?? -1) - (b.altitude ?? -1);
    default:
      return 0;
  }
}

export function sortAlertsBy(alerts: Alert[], field: AlertSortField, direction: SortDirection): Alert[] {
  const mult = direction === 'asc' ? 1 : -1;
  return [...alerts].sort((a, b) => {
    const cmp = compareAlertsByField(a, b, field);
    if (cmp !== 0) return cmp * mult;
    return compareAlertsByField(a, b, 'activated') * mult;
  });
}

export function loadAlertSort(view: 'live' | 'history'): {
  field: AlertSortField;
  direction: SortDirection;
} {
  const parsed = readStoredJson<{ field?: string; direction?: SortDirection }>(`alertSort:${view}`);
  const raw = parsed?.field ?? '';
  const field: AlertSortField = ALERT_SORT_OPTIONS.some((o) => o.value === raw)
    ? (raw as AlertSortField)
    : (LEGACY_ALERT_SORT[raw] ?? 'activated');
  const direction =
    parsed?.direction === 'asc' || parsed?.direction === 'desc'
      ? parsed.direction
      : defaultAlertSortDirection(field);
  return { field, direction };
}

export function saveAlertSort(
  view: 'live' | 'history',
  field: AlertSortField,
  direction: SortDirection,
): void {
  writeStored(`alertSort:${view}`, JSON.stringify({ field, direction }));
}

export const ALERT_SORT_STORE = {
  load: loadAlertSort,
  save: saveAlertSort,
  defaultDirection: defaultAlertSortDirection,
};

/**
 * Trailing text in an alert row: the sorted-by value when it isn't already
 * visible in the row, otherwise the episode time span.
 */
export function alertSortValueLabel(alert: Alert, sortField: AlertSortField): string {
  switch (sortField) {
    case 'duration':
      return formatEpisodeDuration(alert.activated_at, alert.deactivated_at);
    case 'status':
      return isAlertActive(alert) ? 'Active' : 'Ended';
    case 'icao':
      return displayIcao(alert.icao);
    case 'altitude':
      return formatAltitudeCompact(alert.altitude);
    case 'activated':
    case 'zone':
    case 'callsign':
    default:
      return formatEpisodeSpan(alert);
  }
}
