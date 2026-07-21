import type { FlightSummary } from '../api/types';
import type { FlightSortField } from '../utils/flightData';
import { isFiniteNumber, isFlightLive } from '../utils/format';

export function LevelBadge({ flight, alertCount }: { flight: FlightSummary; alertCount?: number }) {
  const level = (flight.level || '').toLowerCase();
  if (level === 'warn') return <span className="level-badge warn">Warn</span>;
  if (level === 'alert') return <span className="level-badge alert">Alert</span>;
  const count = alertCount ?? 0;
  const label = count === 1 ? '1 alert' : `${count} alerts`;
  return <span className={`level-badge ${count > 0 ? 'warn' : 'done'}`}>{label}</span>;
}

export function AlertLevelBadge({ level }: { level?: string }) {
  const normalized = (level || 'event').toLowerCase();
  const display = normalized.charAt(0).toUpperCase() + normalized.slice(1);
  return <span className={`level-badge ${normalized}`}>{display}</span>;
}

export function flightSortValueLabel(flight: FlightSummary, sortField: FlightSortField): string {
  switch (sortField) {
    case 'altitude':
      return isFiniteNumber(flight.altitude)
        ? `${Math.round(Number(flight.altitude)).toLocaleString('en-US')} m`
        : 'N/A';
    case 'speed':
      return isFiniteNumber(flight.speed)
        ? `${Math.round(Number(flight.speed)).toLocaleString('en-US')} km/h`
        : 'N/A';
    case 'zone':
      return flight.zone?.trim() || 'N/A';
    case 'level':
      return flight.level?.trim() || 'N/A';
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
      // last_seen, first_seen: fall through to time label
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
