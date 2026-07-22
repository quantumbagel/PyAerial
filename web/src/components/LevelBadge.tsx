import type { ActiveAlert, FlightSummary } from '../api/types';
import type { FlightSortField } from '../utils/flightData';
import {
  flightAlertSeverity,
  formatDuration,
  formatFlightAlertSummary,
  isFiniteNumber,
  isFlightLive,
  normalizeAlertRule,
} from '../utils/format';

export function LevelBadge({ flight }: { flight: FlightSummary }) {
  const active = flight.active_alerts ?? [];
  const severity = flightAlertSeverity(active);

  if (active.length > 0) {
    const label = isFlightLive(flight)
      ? severity === 'alert'
        ? 'Alert'
        : severity === 'warn'
        ? 'Warn'
        : 'Info'
      : formatFlightAlertSummary(flight);
    const className = severity ? `level-badge ${severity}` : 'level-badge warn';
    return (
      <span className={className}>
        <span className="pulse-dot" aria-hidden="true" />
        {label}
      </span>
    );
  }

  if ((flight.alert_stats?.episode_count ?? 0) > 0) {
    return <span className="level-badge done">{formatFlightAlertSummary(flight)}</span>;
  }

  return <span className="level-badge done">Clear</span>;
}

export function AlertStatusBadge({ active }: { active?: boolean }) {
  if (active) {
    return (
      <span className="alert-status-badge active" title="Alert episode is currently active">
        <span className="pulse-dot" aria-hidden="true" />
        LIVE
      </span>
    );
  }
  return (
    <span className="alert-status-badge ended" title="Alert episode has ended">
      ENDED
    </span>
  );
}

export function AlertRuleBadge({ rule }: { rule?: string }) {
  const norm = normalizeAlertRule(rule);
  const raw = (rule || '').trim();
  const display = raw
    ? raw.charAt(0).toUpperCase() + raw.slice(1).toLowerCase()
    : norm === 'alert'
    ? 'Alert'
    : norm === 'warn'
    ? 'Warn'
    : 'Info';
  return <span className={`level-badge ${norm}`}>{display}</span>;
}

/** @deprecated use AlertRuleBadge */
export const AlertLevelBadge = AlertRuleBadge;

export function flightSortValueLabel(
  flight: FlightSummary,
  sortField: FlightSortField,
  alertCount?: number,
): string {
  switch (sortField) {
    case 'alerts': {
      if ((flight.alert_stats?.episode_count ?? 0) > 0 || (flight.active_alerts?.length ?? 0) > 0) {
        return formatFlightAlertSummary(flight);
      }
      const count = alertCount ?? 0;
      return count === 1 ? '1 active' : `${count} active`;
    }
    case 'altitude':
      return isFiniteNumber(flight.altitude)
        ? `${Math.round(Number(flight.altitude)).toLocaleString('en-US')} m`
        : 'N/A';
    case 'speed':
      return isFiniteNumber(flight.speed)
        ? `${Math.round(Number(flight.speed)).toLocaleString('en-US')} km/h`
        : 'N/A';
    case 'callsign':
      return flight.callsign?.trim() || 'N/A';
    case 'icao':
      return flight.icao?.toUpperCase().trim() || 'N/A';
    case 'model':
      return flight.model?.trim() || 'N/A';
    case 'type':
      return (flight.aircraft_type || flight.typecode)?.trim() || 'N/A';
    case 'duration': {
      const start = flight.start_time ?? 0;
      const end = flight.end_time ?? flight.timestamp ?? start;
      const secs = Math.max(0, end - start);
      return formatDuration(secs);
    }
    default:
      return flightTimeLabel(flight);
  }
}

export function flightTimeLabel(flight: FlightSummary): string {
  const isLive = isFlightLive(flight);
  const formatTime = (ts?: number) => {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
    });
  };

  if (isLive) {
    const liveTime = flight.timestamp ?? flight.end_time ?? flight.start_time;
    return formatTime(liveTime);
  } else {
    const startStr = formatTime(flight.start_time);
    const endStr = formatTime(flight.end_time);
    if (startStr && endStr) {
      return `${startStr} : ${endStr}`;
    }
    return startStr || endStr || '';
  }
}

export function formatActiveAlertLabel(alert: ActiveAlert): string {
  const zone = (alert.zone || '').trim() || 'zone';
  const rule = (alert.rule || '').trim() || 'rule';
  return `${zone} · ${rule}`;
}
