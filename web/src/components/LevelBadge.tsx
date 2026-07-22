import type { ActiveAlert, FlightSummary } from '../api/types';
import type { FlightSortField } from '../utils/flightData';
import { flightAlertSeverity, isFiniteNumber, isFlightLive, normalizeAlertRule } from '../utils/format';

export function LevelBadge({ flight }: { flight: FlightSummary }) {
  const alerts = flight.active_alerts ?? [];
  const count = alerts.length;
  const severity = flightAlertSeverity(alerts);
  if (severity === 'alert') return <span className="level-badge alert">Alert</span>;
  if (severity === 'warn') return <span className="level-badge warn">Warn</span>;
  if (count > 0) {
    const label = count === 1 ? '1 active' : `${count} active`;
    return <span className="level-badge warn">{label}</span>;
  }
  return <span className="level-badge done">Clear</span>;
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
      const count = flight.active_alerts?.length ?? alertCount ?? 0;
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
      const m = Math.floor(secs / 60);
      const s = Math.floor(secs % 60);
      return secs > 0 ? `${m}m ${s}s` : 'N/A';
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
