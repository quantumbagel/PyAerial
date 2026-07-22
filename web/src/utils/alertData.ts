import type { Alert } from '../api/types';
import {
  formatActiveSince,
  formatEpisodeDuration,
  formatZoneRule,
  isAlertActive,
  isFiniteNumber,
} from './format';

export type AlertSortField =
  | 'activated'
  | 'active'
  | 'duration'
  | 'status'
  | 'zone'
  | 'rule'
  | 'callsign'
  | 'icao'
  | 'altitude';

export type SortDirection = 'asc' | 'desc';

/** Stable identity for merging live updates into an existing episode row. */
export function alertEpisodeIdentity(alert: Alert): string {
  if (alert.alert_id && alert.activated_at != null) {
    return `${alert.alert_id}:${alert.activated_at}`;
  }
  return alertEpisodeKey(alert);
}

/** Merge incoming alert snapshots into an existing list by episode identity. */
export function mergeAlertsByEpisode(existing: Alert[], updates: Alert[]): Alert[] {
  if (updates.length === 0) {
    return existing;
  }
  const updateMap = new Map(updates.map((alert) => [alertEpisodeIdentity(alert), alert]));
  const mergedKeys = new Set<string>();
  const merged = existing.map((alert) => {
    const key = alertEpisodeIdentity(alert);
    const update = updateMap.get(key);
    if (update) {
      mergedKeys.add(key);
      return { ...alert, ...update };
    }
    return alert;
  });
  for (const alert of updates) {
    const key = alertEpisodeIdentity(alert);
    if (!mergedKeys.has(key) && !merged.some((item) => alertEpisodeIdentity(item) === key)) {
      merged.push(alert);
      mergedKeys.add(key);
    }
  }
  return merged;
}

/** Unique key for a single alert episode row (alert_id repeats across re-entries and event records). */
export function alertEpisodeKey(alert: Alert, fallbackIndex?: number): string {
  const activated = alert.activated_at;
  const deactivatedPart =
    alert.deactivated_at != null && alert.deactivated_at > 0
      ? String(alert.deactivated_at)
      : alert.active === false
        ? 'ended'
        : 'active';

  if (alert.alert_id && activated != null) {
    return `${alert.alert_id}:${activated}:${deactivatedPart}`;
  }
  if (alert.alert_id) {
    return `${alert.alert_id}:idx:${fallbackIndex ?? 0}`;
  }
  if (activated != null) {
    return `episode:${activated}:${deactivatedPart}`;
  }
  return `episode-index:${fallbackIndex ?? 0}`;
}

export const ALERT_SORT_OPTIONS: { value: AlertSortField; label: string }[] = [
  { value: 'activated', label: 'Activated' },
  { value: 'active', label: 'Active' },
  { value: 'duration', label: 'Duration' },
  { value: 'status', label: 'Status' },
  { value: 'zone', label: 'Zone' },
  { value: 'rule', label: 'Rule' },
  { value: 'callsign', label: 'Callsign' },
  { value: 'icao', label: 'ICAO' },
  { value: 'altitude', label: 'Altitude' },
];

export function defaultAlertSortDirection(field: AlertSortField): SortDirection {
  if (field === 'zone' || field === 'rule' || field === 'callsign' || field === 'icao') {
    return 'asc';
  }
  return 'desc';
}

function alertDuration(alert: Alert, now = Date.now() / 1000): number {
  if (!alert.activated_at) return 0;
  const end = alert.deactivated_at ?? now;
  return Math.max(0, end - alert.activated_at);
}

function compareAlertsByField(a: Alert, b: Alert, field: AlertSortField): number {
  switch (field) {
    case 'activated':
      return (a.activated_at ?? 0) - (b.activated_at ?? 0);
    case 'duration':
      return alertDuration(a) - alertDuration(b);
    case 'active':
    case 'status': {
      const aActive = isAlertActive(a) ? 1 : 0;
      const bActive = isAlertActive(b) ? 1 : 0;
      return aActive - bActive;
    }
    case 'zone':
      return (a.zone || '').localeCompare(b.zone || '', undefined, { sensitivity: 'base' });
    case 'rule':
      return (a.rule || '').localeCompare(b.rule || '', undefined, { sensitivity: 'base' });
    case 'callsign':
      return (a.callsign || '').localeCompare(b.callsign || '', undefined, { sensitivity: 'base' });
    case 'icao':
      return (a.icao || '').localeCompare(b.icao || '', undefined, { sensitivity: 'base' });
    case 'altitude':
      return (a.altitude ?? -1) - (b.altitude ?? -1);
    default:
      return 0;
  }
}

function compareAlertsByActivated(a: Alert, b: Alert, direction: SortDirection): number {
  const cmp = compareAlertsByField(a, b, 'activated');
  return direction === 'asc' ? cmp : -cmp;
}

export function sortAlertsBy(
  alerts: Alert[],
  field: AlertSortField,
  direction: SortDirection,
): Alert[] {
  const mult = direction === 'asc' ? 1 : -1;
  return [...alerts].sort((a, b) => {
    const cmp = compareAlertsByField(a, b, field);
    if (cmp !== 0) return cmp * mult;
    return compareAlertsByActivated(a, b, direction);
  });
}

export function loadAlertSort(view: 'live' | 'history'): {
  field: AlertSortField;
  direction: SortDirection;
} {
  try {
    const raw = localStorage.getItem(`alertSort:${view}`);
    if (!raw) return { field: 'activated', direction: 'desc' };
    const parsed = JSON.parse(raw) as { field?: AlertSortField; direction?: SortDirection };
    const field = ALERT_SORT_OPTIONS.some((o) => o.value === parsed.field)
      ? parsed.field!
      : 'activated';
    const direction =
      parsed.direction === 'asc' || parsed.direction === 'desc'
        ? parsed.direction
        : defaultAlertSortDirection(field);
    return { field, direction };
  } catch {
    return { field: 'activated', direction: 'desc' };
  }
}

export function saveAlertSort(
  view: 'live' | 'history',
  field: AlertSortField,
  direction: SortDirection,
): void {
  localStorage.setItem(`alertSort:${view}`, JSON.stringify({ field, direction }));
}

export function alertSortValueLabel(alert: Alert, sortField: AlertSortField): string {
  switch (sortField) {
    case 'duration':
      return formatEpisodeDuration(alert.activated_at, alert.deactivated_at);
    case 'active': {
      const activatedStr = formatActiveSince(alert.activated_at);
      return isAlertActive(alert) ? `Active · ${activatedStr}` : `Ended · ${activatedStr}`;
    }
    case 'status':
      return isAlertActive(alert) ? 'Active' : 'Ended';
    case 'zone':
    case 'rule':
      return formatZoneRule(alert.zone, alert.rule);
    case 'callsign': {
      const raw = alert.callsign?.trim();
      return raw && raw.toUpperCase() !== 'UNKNOWN' ? raw : (alert.icao || '').toUpperCase() || 'N/A';
    }
    case 'icao':
      return (alert.icao || '').toUpperCase() || 'N/A';
    case 'altitude':
      return isFiniteNumber(alert.altitude)
        ? `${Math.round(Number(alert.altitude)).toLocaleString('en-US')} m`
        : 'N/A';
    case 'activated':
    default: {
      const activatedStr = formatActiveSince(alert.activated_at);
      const deactivatedStr = alert.deactivated_at ? formatActiveSince(alert.deactivated_at) : null;
      const durationStr = formatEpisodeDuration(alert.activated_at, alert.deactivated_at);
      return deactivatedStr
        ? `${activatedStr} – ${deactivatedStr} (${durationStr})`
        : `${activatedStr} (${durationStr})`;
    }
  }
}
