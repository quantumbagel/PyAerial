import type { Alert } from '../api/types';
import { formatActiveSince, formatAlertAltitude, formatAlertEta, normalizeAlertRule } from '../utils/format';
import { AlertRuleBadge } from './LevelBadge';

interface AlertListItemProps {
  alert: Alert;
  active: boolean;
  onSelect: (alert: Alert) => void;
}

export function AlertListItem({ alert, active, onSelect }: AlertListItemProps) {
  const normLevel = normalizeAlertRule(alert.rule);
  const timeStr = formatActiveSince(alert.activated_at);
  const latVal = alert.latitude != null ? alert.latitude.toFixed(5) : 'N/A';
  const lonVal = alert.longitude != null ? alert.longitude.toFixed(5) : 'N/A';
  const title = `Active since ${alert.activated_at ? new Date(alert.activated_at * 1000).toLocaleString() : 'N/A'}\nPosition: ${latVal}, ${lonVal}\nAltitude: ${formatAlertAltitude(alert.altitude)}\nETA: ${formatAlertEta(alert.eta)}`;

  const rawCallsign = alert.callsign?.trim();
  const displayCallsign =
    rawCallsign && rawCallsign.toUpperCase() !== 'UNKNOWN'
      ? rawCallsign
      : (alert.icao || '').toUpperCase() || 'Loading plane details…';

  const zoneRule = `${alert.zone || 'zone'} · ${alert.rule || 'rule'}`;

  return (
    <li>
      <button
        type="button"
        className={`list-row alert-item ${normLevel}${active ? ' active' : ''}`}
        title={title}
        onClick={() => onSelect(alert)}
        aria-pressed={active}
      >
        <div className="flight-meta-row">
          <span className="flight-callsign">
            {displayCallsign}{' '}
            <AlertRuleBadge rule={alert.rule} />
          </span>
          <span className="flight-icao">{(alert.icao || '').toUpperCase()}</span>
        </div>
        <div className="flight-meta-row">
          <span className="flight-desc">{zoneRule}</span>
          <span className="flight-time">{timeStr}</span>
        </div>
      </button>
    </li>
  );
}
