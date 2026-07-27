import type { FlightSummary, Zone } from '../api/types';
import type { FlightSortField } from '../utils/flightData';
import {
  formatDuration,
  formatFlightAlertSummary,
  formatZoneRule,
  isFiniteNumber,
  isFlightLive,
} from '../utils/format';
import { alertColorFor, getAccessibleBadgeTextColor } from '../utils/zoneColors';
import { Badge, BadgeGroup } from './ui';

export function LiveBadge() {
  return <Badge variant="live">Live</Badge>;
}

export function AlertColoredLabel({
  zone,
  rule,
  zones,
  alertColors,
  className,
  active = false,
}: {
  zone: string;
  rule?: string;
  zones?: Zone[];
  alertColors?: Record<string, string>;
  className?: string;
  active?: boolean;
}) {
  const zoneName = (zone || 'zone').trim() || 'zone';
  const ruleName = (rule || '').trim() || 'rule';
  const colors = alertColorFor(zoneName, ruleName, zones, alertColors);
  return (
    <ZoneBadge
      zone={zoneName}
      rule={ruleName}
      zones={zones}
      alertColors={alertColors}
      className={className}
      label={formatZoneRule(zoneName, ruleName, { live: active })}
      colors={colors}
    />
  );
}

export function ZoneBadge({
  zone,
  rule,
  zones,
  alertColors,
  className,
  label,
  colors: colorsOverride,
}: {
  zone: string;
  rule?: string;
  zones?: Zone[];
  alertColors?: Record<string, string>;
  className?: string;
  label?: string;
  colors?: { fill: string };
}) {
  const zoneName = (zone || 'zone').trim() || 'zone';
  const ruleName = (rule || '').trim() || 'rule';
  const displayLabel = label ?? formatZoneRule(zoneName, ruleName);
  const colors = colorsOverride ?? alertColorFor(zoneName, ruleName, zones, alertColors);
  const textColor = getAccessibleBadgeTextColor(colors.fill);
  return (
    <Badge
      variant="zone"
      className={className}
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

/** @deprecated use ZoneBadge with zone name */
export function AlertRuleBadge({
  rule,
  zone,
  zones,
  alertColors,
}: {
  rule?: string;
  zone?: string;
  zones?: Zone[];
  alertColors?: Record<string, string>;
}) {
  return <ZoneBadge zone={zone || 'zone'} rule={rule} zones={zones} alertColors={alertColors} />;
}

/** @deprecated use LiveBadge or omit for ended episodes */
export function AlertStatusBadge({ active }: { active?: boolean }) {
  if (!active) return null;
  return <LiveBadge />;
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
      return flightTimeLabel(flight, sortField);
  }
}

export function flightTimeLabel(flight: FlightSummary, sortField?: FlightSortField): string {
  const isLive = isFlightLive(flight);
  const formatTime = (ts?: number) => {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
    });
  };

  if (sortField === 'first_seen') {
    return formatTime(flight.start_time) || formatTime(flight.timestamp ?? flight.end_time);
  }

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
