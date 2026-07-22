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
        const isEpisodeActive = isAlertActive(alert);
        const activatedStr = formatActiveSince(alert.activated_at);
        const deactivatedStr = alert.deactivated_at
          ? formatActiveSince(alert.deactivated_at)
          : null;
        const durationStr = formatEpisodeDuration(alert.activated_at, alert.deactivated_at);
        const timeLabel = isEpisodeActive
          ? `Started ${activatedStr}`
          : deactivatedStr
            ? `${activatedStr} – ${deactivatedStr}`
            : `Activated ${activatedStr}`;
        const latVal = alert.latitude != null ? alert.latitude.toFixed(5) : 'N/A';
        const lonVal = alert.longitude != null ? alert.longitude.toFixed(5) : 'N/A';
        const title = `${isEpisodeActive ? 'LIVE' : 'ENDED'} Episode · ${durationStr}\nActivated: ${alert.activated_at ? new Date(alert.activated_at * 1000).toLocaleString() : 'N/A'}\nPosition: ${latVal}, ${lonVal}\nAltitude: ${formatAlertAltitude(alert.altitude)}${isEpisodeActive ? `\nETA: ${formatAlertEta(alert.eta)}` : ''}`;

        return (
          <button
            type="button"
            key={alert.alert_id}
            data-alert-id={alert.alert_id}
            className={`alert-timeline-item ${normLevel}${isEpisodeActive ? ' episode-active' : ' episode-ended'}${alert.alert_id === activeAlertId ? ' active' : ''}`}
            title={title}
            onClick={() => onSelectAlert(alert)}
            aria-pressed={alert.alert_id === activeAlertId}
          >
            <div className="alert-timeline-row">
              <div className="alert-timeline-badges">
                <AlertRuleBadge rule={alert.rule} />
                <AlertStatusBadge active={isEpisodeActive} />
              </div>
              <span className="alert-timeline-time">{timeLabel}</span>
            </div>
            <div className="alert-timeline-zone">
              <span className="alert-timeline-zone-name">
                {alert.zone || 'zone'} · {alert.rule || 'rule'}
              </span>
            </div>
            <div className="alert-timeline-meta">
              <span><strong>Duration:</strong> {durationStr}</span>
              <span><strong>Alt:</strong> {formatAlertAltitude(alert.altitude)}</span>
              {isEpisodeActive ? (
                <span><strong>ETA:</strong> {formatAlertEta(alert.eta)}</span>
              ) : (
                <span className="alert-timeline-cleared-tag">Episode Cleared</span>
              )}
            </div>
          </button>
        );
      })}
    </div>
  );
}

