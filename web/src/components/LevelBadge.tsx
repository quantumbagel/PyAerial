import type { FlightSummary, Zone } from '../api/types';
import { formatFlightAlertSummary, formatZoneRule, isFlightLive } from '../utils/format';
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
