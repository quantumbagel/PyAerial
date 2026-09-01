import type { FlightSummary, Zone } from '../api/types';
import type { FlightSortField } from '../utils/flightData';
import {
  formatDate,
  formatDateTime,
  formatDuration,
  formatFlightAlertSummary,
  formatTime,
  formatZoneRule,
  isFiniteNumber,
  isFlightLive,
  isSameLocalDay,
} from '../utils/format';
import { alertColorFor, getAccessibleBadgeTextColor } from '../utils/zoneColors';
import { Badge, BadgeGroup } from './ui';

export function ZoneBadge({
  zone,
  rule,
  zones,
  alertColors,
  label,
}: {
  zone: string;
  rule?: string;
  zones?: Zone[];
  alertColors?: Record<string, string>;
  label?: string;
}) {
  const zoneName = (zone || 'zone').trim() || 'zone';
  const ruleName = (rule || '').trim() || 'rule';
  const displayLabel = label ?? formatZoneRule(zoneName, ruleName);
  const colors = alertColorFor(zoneName, ruleName, zones, alertColors);
  const textColor = getAccessibleBadgeTextColor(colors.fill);
  return (
    <Badge
      variant="zone"
      style={{
        backgroundColor: `${colors.fill}26`,
        color: textColor,
        borderColor: `${colors.fill}55`,
      }}
    >
      {displayLabel}
    </Badge>
  );
}

interface LevelBadgeProps {
  flight: FlightSummary;
  zones?: Zone[];
  alertColors?: Record<string, string>;
}

export function LevelBadge({ flight, zones, alertColors }: LevelBadgeProps) {
  const active = flight.active_alerts ?? [];

  if (active.length > 0) {
    return (
      <BadgeGroup>
        {active.map((alert, index) => (
          <ZoneBadge
            key={alert.alert_id ?? `${alert.zone}-${alert.rule}-${index}`}
            zone={alert.zone}
            rule={alert.rule}
            zones={zones}
            alertColors={alertColors}
            label={formatZoneRule(alert.zone, alert.rule, { live: true })}
          />
        ))}
      </BadgeGroup>
    );
  }

  if (isFlightLive(flight)) {
    return null;
  }

  if ((flight.alert_stats?.episode_count ?? 0) > 0) {
    return <Badge variant="neutral">{formatFlightAlertSummary(flight)}</Badge>;
  }

  return <Badge variant="neutral">Clear</Badge>;
}

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
      return flight.aircraft_type?.trim() || 'N/A';
    case 'duration': {
      const start = flight.start_time ?? 0;
      const end = flight.end_time ?? flight.timestamp ?? start;
      const secs = Math.max(0, end - start);
      return formatDuration(secs);
    }
    default:
      return flightTimeLabel(flight, sortField);
  }
}

export function flightTimeLabel(flight: FlightSummary, sortField?: FlightSortField): string {
  const isLive = isFlightLive(flight);
  const formatTs = (ts?: number) => (ts ? formatDateTime(ts) : '');

  if (sortField === 'first_seen') {
    return formatTs(flight.start_time) || formatTs(flight.timestamp ?? flight.end_time);
  }

  if (isLive) {
    const liveTime = flight.timestamp ?? flight.end_time ?? flight.start_time;
    return formatTs(liveTime);
  }

  const start = flight.start_time;
  const end = flight.end_time;
  if (start && end) {
    if (isSameLocalDay(start, end)) {
      return `${formatDate(start)} · ${formatTime(start)} – ${formatTime(end)}`;
    }
    return `${formatDateTime(start)} : ${formatDateTime(end)}`;
  }
  return formatTs(start ?? end);
}
