import type { Alert } from '../api/types';
import { formatAlertAltitude, formatAlertEta, normalizeAlertLevel } from '../utils/format';
import { AlertLevelBadge } from './LevelBadge';

interface AlertListItemProps {
  alert: Alert;
  active: boolean;
  onSelect: (alert: Alert) => void;
}

export function AlertListItem({ alert, active, onSelect }: AlertListItemProps) {
  const normLevel = normalizeAlertLevel(alert.level);
  const timeStr = alert.timestamp
    ? new Date(alert.timestamp * 1000).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })
    : '';
  const latVal = alert.latitude != null ? alert.latitude.toFixed(5) : 'N/A';
  const lonVal = alert.longitude != null ? alert.longitude.toFixed(5) : 'N/A';
  const title = `Triggered:\nTime: ${alert.timestamp ? new Date(alert.timestamp * 1000).toLocaleString() : 'N/A'}\nPosition: ${latVal}, ${lonVal}\nAltitude: ${formatAlertAltitude(alert.altitude)}\nETA: ${formatAlertEta(alert.eta)}`;

  const rawCallsign = alert.callsign?.trim();
  const displayCallsign =
    rawCallsign && rawCallsign.toUpperCase() !== 'UNKNOWN'
      ? rawCallsign
      : (alert.icao || '').toUpperCase() || 'Loading plane details…';

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
            <AlertLevelBadge level={alert.level} />
          </span>
          <span className="flight-icao">{(alert.icao || '').toUpperCase()}</span>
        </div>
        <div className="flight-meta-row">
          <span className="flight-desc">{alert.zone || 'Zone'}</span>
          <span className="flight-time">{timeStr}</span>
        </div>
      </button>
    </li>
  );
}
