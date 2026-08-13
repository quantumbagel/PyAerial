import type { Alert, Zone } from '../api/types';
import { alertSortValueLabel, type AlertSortField } from '../utils/alertData';
import {
  formatActiveSince,
  formatAlertAltitude,
  formatAlertEta,
  formatDateTime,
  formatEpisodeDuration,
  formatZoneRule,
  isAlertActive,
} from '../utils/format';
import { ZoneBadge } from './LevelBadge';
import { Chip } from './ui';

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
  const isEpisodeActive = isAlertActive(alert);
  const deactivatedStr = alert.deactivated_at ? formatActiveSince(alert.deactivated_at) : null;
  const latVal = alert.latitude != null ? alert.latitude.toFixed(5) : 'N/A';
  const lonVal = alert.longitude != null ? alert.longitude.toFixed(5) : 'N/A';
  const durationStr = formatEpisodeDuration(alert.activated_at, alert.deactivated_at);

  const statusTitle = isEpisodeActive
    ? `Active episode · ${durationStr}`
    : `Ended episode · ${durationStr}${deactivatedStr ? ` (ended ${deactivatedStr})` : ''}`;
  const title = `${statusTitle}\nActivated: ${formatDateTime(alert.activated_at) || 'N/A'}\nPosition: ${latVal}, ${lonVal}\nAltitude: ${formatAlertAltitude(alert.altitude)}${isEpisodeActive ? `\nETA: ${formatAlertEta(alert.eta)}` : ''}`;

  const rawCallsign = alert.callsign?.trim();
  const displayCallsign =
    rawCallsign && rawCallsign.toUpperCase() !== 'UNKNOWN'
      ? rawCallsign
      : (alert.icao || '').toUpperCase() || 'Loading plane details…';

  return (
    <li>
      <button
        type="button"
        className={`ui-row${!isEpisodeActive ? ' ui-row--muted' : ''}${active ? ' is-active' : ''}`}
        title={title}
        onClick={() => onSelect(alert, episodeKey)}
        aria-pressed={active}
      >
        <div className="ui-row__line">
          <span className="ui-row__title">{displayCallsign}</span>
          <Chip>{(alert.icao || '').toUpperCase()}</Chip>
        </div>
        <div className="ui-row__line">
          <ZoneBadge
            zone={alert.zone || 'zone'}
            rule={alert.rule}
            zones={zones}
            alertColors={alertColors}
            label={formatZoneRule(alert.zone || 'zone', alert.rule, { live: isEpisodeActive })}
          />
          <span className="ui-row__trailing">{alertSortValueLabel(alert, sortField)}</span>
        </div>
      </button>
    </li>
  );
}
