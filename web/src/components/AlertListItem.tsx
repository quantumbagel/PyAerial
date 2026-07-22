import type { Alert } from '../api/types';
import {
  formatActiveSince,
  formatAlertAltitude,
  formatAlertEta,
  formatEpisodeDuration,
  isAlertActive,
  normalizeAlertRule,
} from '../utils/format';
import { AlertRuleBadge, AlertStatusBadge } from './LevelBadge';

interface AlertListItemProps {
  alert: Alert;
  active: boolean;
  onSelect: (alert: Alert) => void;
}

export function AlertListItem({ alert, active, onSelect }: AlertListItemProps) {
  const normLevel = normalizeAlertRule(alert.rule);
  const isEpisodeActive = isAlertActive(alert);
  const timeStr = formatActiveSince(alert.activated_at);
  const deactivatedStr = alert.deactivated_at ? formatActiveSince(alert.deactivated_at) : null;
  const latVal = alert.latitude != null ? alert.latitude.toFixed(5) : 'N/A';
  const lonVal = alert.longitude != null ? alert.longitude.toFixed(5) : 'N/A';
  const durationStr = formatEpisodeDuration(alert.activated_at, alert.deactivated_at);

  const statusTitle = isEpisodeActive
    ? `LIVE Episode · Active for ${durationStr}`
    : `ENDED Episode · Total duration ${durationStr}${deactivatedStr ? ` (ended ${deactivatedStr})` : ''}`;
  const title = `${statusTitle}\nActivated: ${alert.activated_at ? new Date(alert.activated_at * 1000).toLocaleString() : 'N/A'}\nPosition: ${latVal}, ${lonVal}\nAltitude: ${formatAlertAltitude(alert.altitude)}${isEpisodeActive ? `\nETA: ${formatAlertEta(alert.eta)}` : ''}`;

  const rawCallsign = alert.callsign?.trim();
  const displayCallsign =
    rawCallsign && rawCallsign.toUpperCase() !== 'UNKNOWN'
      ? rawCallsign
      : (alert.icao || '').toUpperCase() || 'Loading plane details…';

  const zoneRule = `${alert.zone || 'zone'} · ${alert.rule || 'rule'}`;
  const timeDisplay = isEpisodeActive
    ? `${timeStr} · ${durationStr} ongoing`
    : deactivatedStr
    ? `${timeStr} – ${deactivatedStr} (${durationStr})`
    : `${timeStr} (${durationStr})`;

  return (
    <li>
      <button
        type="button"
        className={`list-row alert-item ${normLevel}${isEpisodeActive ? ' alert-episode-active' : ' alert-episode-ended'}${active ? ' active' : ''}`}
        title={title}
        onClick={() => onSelect(alert)}
        aria-pressed={active}
      >
        <div className="flight-meta-row">
          <span className="flight-callsign">
            {displayCallsign}{' '}
            <AlertRuleBadge rule={alert.rule} />
            <AlertStatusBadge active={isEpisodeActive} />
          </span>
          <span className="flight-icao">{(alert.icao || '').toUpperCase()}</span>
        </div>
        <div className="flight-meta-row">
          <span className="flight-desc">{zoneRule}</span>
          <span className="flight-time">{timeDisplay}</span>
        </div>
      </button>
    </li>
  );
}

