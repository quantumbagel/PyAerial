import type { CSSProperties } from 'react';
import type { AlertStats, ActiveAlert, Integrity, Zone } from '../api/types';
import { alertVisual } from '../utils/alertData';
import { integrityLabel, isFlagged } from '../utils/integrity';
import { pluralize } from '../utils/format';
import { Badge, BadgeGroup } from './ui';

interface ZoneBadgeProps {
  zone?: string | null;
  rule?: string | null;
  /** Active episodes get a status dot (and screen-reader text). */
  active?: boolean;
  zones?: Zone[];
  alertColors?: Record<string, string>;
}

/** "Zone · rule" in the alert's configured colour. */
export function ZoneBadge({ zone, rule, active = false, zones, alertColors }: ZoneBadgeProps) {
  const visual = alertVisual({ zone, rule }, zones, alertColors);
  return (
    <Badge
      variant="zone"
      className={active ? 'ui-badge--active' : undefined}
      style={{ '--tone': visual.color, '--tone-text': visual.text } as CSSProperties}
    >
      {active && <span className="ui-badge__dot" aria-hidden="true" />}
      {visual.label}
      {active && <span className="visually-hidden"> (active)</span>}
    </Badge>
  );
}

interface FlightAlertBadgesProps {
  flight: { active_alerts?: ActiveAlert[] | null; alert_stats?: Partial<AlertStats> | null };
  zones?: Zone[];
  alertColors?: Record<string, string>;
}

/**
 * A flight's alert state, the same in Live and History: its active alerts,
 * else a count of past episodes, else nothing.
 */
export function FlightAlertBadges({ flight, zones, alertColors }: FlightAlertBadgesProps) {
  const active = flight.active_alerts ?? [];
  if (active.length > 0) {
    return (
      <BadgeGroup>
        {active.map((alert) => (
          <ZoneBadge
            key={alert.alert_id}
            zone={alert.zone}
            rule={alert.rule}
            active
            zones={zones}
            alertColors={alertColors}
          />
        ))}
      </BadgeGroup>
    );
  }
  const past = flight.alert_stats?.episode_count ?? 0;
  if (past > 0) {
    return <Badge variant="neutral">{pluralize(past, 'past alert')}</Badge>;
  }
  return null;
}

/** "Suspect" / "Likely spoofed" when the integrity checks disagree with the track. */
export function IntegrityBadge({ integrity }: { integrity?: Integrity | null }) {
  if (!isFlagged(integrity)) return null;
  const spoofed = integrity?.status === 'spoofed';
  const tone = spoofed ? 'var(--alert)' : 'var(--warn)';
  return (
    <Badge
      variant="zone"
      style={{ '--tone': tone, '--tone-text': tone } as CSSProperties}
    >
      {integrityLabel(integrity)}
    </Badge>
  );
}
