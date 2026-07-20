import type { FlightSummary } from '../api/types';
import { isFlightLive } from '../utils/format';

export function LevelBadge({ flight, alertCount }: { flight: FlightSummary; alertCount?: number }) {
  if (flight.is_live) {
    return <span className="level-badge live">Live</span>;
  }
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
