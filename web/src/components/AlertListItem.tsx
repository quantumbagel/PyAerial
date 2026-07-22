import type { Alert, Zone } from '../api/types';
import { alertSortValueLabel, type AlertSortField } from '../utils/alertData';
import {
  formatActiveSince,
  formatAlertAltitude,
  formatAlertEta,
  formatEpisodeDuration,
  isAlertActive,
  normalizeAlertRule,
} from '../utils/format';
import { AlertColoredLabel } from './LevelBadge';

interface AlertListItemProps {
  alert: Alert;
  episodeKey: string;
  active: boolean;
  sortField: AlertSortField;
  zones?: Zone[];
  alertColors?: Record<string, string>;
  onSelect: (alert: Alert, episodeKey: string) => void;
}

export function AlertListItem({
  alert,
  episodeKey,
  active,
  sortField,
  zones,
  alertColors,
  onSelect,
}: AlertListItemProps) {
  const normLevel = normalizeAlertRule(alert.rule);
  const isEpisodeActive = isAlertActive(alert);
  const deactivatedStr = alert.deactivated_at ? formatActiveSince(alert.deactivated_at) : null;
  const latVal = alert.latitude != null ? alert.latitude.toFixed(5) : 'N/A';
  const lonVal = alert.longitude != null ? alert.longitude.toFixed(5) : 'N/A';
  const durationStr = formatEpisodeDuration(alert.activated_at, alert.deactivated_at);

  const statusTitle = isEpisodeActive
    ? `Active episode · ${durationStr}`
    : `Ended episode · ${durationStr}${deactivatedStr ? ` (ended ${deactivatedStr})` : ''}`;
  const title = `${statusTitle}\nActivated: ${alert.activated_at ? new Date(alert.activated_at * 1000).toLocaleString() : 'N/A'}\nPosition: ${latVal}, ${lonVal}\nAltitude: ${formatAlertAltitude(alert.altitude)}${isEpisodeActive ? `\nETA: ${formatAlertEta(alert.eta)}` : ''}`;

  const rawCallsign = alert.callsign?.trim();
  const displayCallsign =
    rawCallsign && rawCallsign.toUpperCase() !== 'UNKNOWN'
      ? rawCallsign
      : (alert.icao || '').toUpperCase() || 'Loading plane details…';

  return (
    <li>
      <button
        type="button"
        className={`list-row alert-item ${normLevel}${isEpisodeActive ? ' alert-episode-active' : ' alert-episode-ended'}${active ? ' active' : ''}`}
        title={title}
        onClick={() => onSelect(alert, episodeKey)}
        aria-pressed={active}
      >
        <div className="flight-meta-row">
          <span className="flight-callsign">{displayCallsign}</span>
          <span className="flight-icao">{(alert.icao || '').toUpperCase()}</span>
        </div>
        <div className="flight-meta-row">
          <AlertColoredLabel
            zone={alert.zone || 'zone'}
            rule={alert.rule}
            zones={zones}
            alertColors={alertColors}
            active={isEpisodeActive}
            className="flight-desc alert-colored-label"
          />
          <span className="flight-time">{alertSortValueLabel(alert, sortField)}</span>
        </div>
      </button>
    </li>
  );
}
