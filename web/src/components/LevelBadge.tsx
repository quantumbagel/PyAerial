import type { FlightSummary } from '../api/types';
import { isFlightLive } from '../utils/format';

export function LevelBadge({ flight }: { flight: FlightSummary }) {
  if (flight.is_live) {
    return <span className="level-badge live">Live</span>;
  }
  const level = (flight.level || '').toLowerCase();
  if (level === 'warn') return <span className="level-badge warn">Warn</span>;
  if (level === 'alert') return <span className="level-badge alert">Alert</span>;
  return <span className="level-badge done">Done</span>;
}

export function AlertLevelBadge({ level }: { level?: string }) {
  const normalized = (level || 'event').toLowerCase();
  const display = normalized.charAt(0).toUpperCase() + normalized.slice(1);
  return <span className={`level-badge ${normalized}`}>{display}</span>;
}

export function StatusDot({ live }: { live?: boolean }) {
  return <span className={`status-dot${live ? ' live' : ''}`} />;
}

export function flightTimeLabel(flight: FlightSummary): string {
  const isLive = isFlightLive(flight);
  if (isLive && flight.timestamp) {
    return new Date(flight.timestamp * 1000).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  }
  if (flight.start_time) {
    return new Date(flight.start_time * 1000).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
    });
  }
  return '';
}
