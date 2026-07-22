import type { Alert } from '../api/types';
import { formatAlertAltitude, formatAlertEta, normalizeAlertLevel } from '../utils/format';
import { AlertLevelBadge } from './LevelBadge';

interface AlertTimelineProps {
  alerts: Alert[];
  activeAlertId: string | null;
  onSelectAlert: (alert: Alert) => void;
}

export function AlertTimeline({ alerts, activeAlertId, onSelectAlert }: AlertTimelineProps) {
  if (alerts.length === 0) {
    return <div className="alert-timeline-empty">No alert events for this flight.</div>;
  }

  return (
    <div id="alert-timeline-list">
      {alerts.map((alert) => {
        const normLevel = normalizeAlertLevel(alert.level);
        const timeStr = alert.timestamp
          ? new Date(Number(alert.timestamp) * 1000).toLocaleTimeString([], {
              hour: '2-digit',
              minute: '2-digit',
              second: '2-digit',
            })
          : '';
        const latVal = alert.latitude != null ? alert.latitude.toFixed(5) : 'N/A';
        const lonVal = alert.longitude != null ? alert.longitude.toFixed(5) : 'N/A';
        const title = `Triggered:\nTime: ${alert.timestamp ? new Date(alert.timestamp * 1000).toLocaleString() : 'N/A'}\nPosition: ${latVal}, ${lonVal}\nAltitude: ${formatAlertAltitude(alert.altitude)}\nETA: ${formatAlertEta(alert.eta)}`;
        return (
          <button
            type="button"
            key={alert.alert_id}
            data-alert-id={alert.alert_id}
            className={`alert-timeline-item ${normLevel}${alert.alert_id === activeAlertId ? ' active' : ''}`}
            title={title}
            onClick={() => onSelectAlert(alert)}
            aria-pressed={alert.alert_id === activeAlertId}
          >
            <div className="alert-timeline-row">
              <AlertLevelBadge level={alert.level} />
              <span className="alert-timeline-time">{timeStr}</span>
            </div>
            <div className="alert-timeline-zone">
              Entered Zone: <span className="alert-timeline-zone-name">{alert.zone || 'zone'}</span>
            </div>
            <div className="alert-timeline-meta">
              <span><strong>Alt:</strong> {formatAlertAltitude(alert.altitude)}</span>
              <span><strong>ETA:</strong> {formatAlertEta(alert.eta)}</span>
            </div>
          </button>
        );
      })}
    </div>
  );
}
