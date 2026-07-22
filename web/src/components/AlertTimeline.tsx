import type { Alert } from '../api/types';
import { formatActiveSince, formatAlertAltitude, formatAlertEta, normalizeAlertRule } from '../utils/format';
import { AlertRuleBadge } from './LevelBadge';

interface AlertTimelineProps {
  alerts: Alert[];
  activeAlertId: string | null;
  onSelectAlert: (alert: Alert) => void;
}

export function AlertTimeline({ alerts, activeAlertId, onSelectAlert }: AlertTimelineProps) {
  if (alerts.length === 0) {
    return <div className="alert-timeline-empty">No alert episodes for this flight.</div>;
  }

  return (
    <div id="alert-timeline-list">
      {alerts.map((alert) => {
        const normLevel = normalizeAlertRule(alert.rule);
        const activatedStr = formatActiveSince(alert.activated_at);
        const deactivatedStr = alert.deactivated_at
          ? formatActiveSince(alert.deactivated_at)
          : null;
        const latVal = alert.latitude != null ? alert.latitude.toFixed(5) : 'N/A';
        const lonVal = alert.longitude != null ? alert.longitude.toFixed(5) : 'N/A';
        const statusLabel = alert.active
          ? `Active since ${activatedStr}`
          : deactivatedStr
            ? `Active ${activatedStr} – ${deactivatedStr}`
            : `Activated ${activatedStr}`;
        const title = `${statusLabel}\nPosition: ${latVal}, ${lonVal}\nAltitude: ${formatAlertAltitude(alert.altitude)}\nETA: ${formatAlertEta(alert.eta)}`;
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
              <AlertRuleBadge rule={alert.rule} />
              <span className="alert-timeline-time">{statusLabel}</span>
            </div>
            <div className="alert-timeline-zone">
              <span className="alert-timeline-zone-name">
                {alert.zone || 'zone'} · {alert.rule || 'rule'}
              </span>
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
