import type { Alert, Zone } from '../api/types';
import { alertEpisodeKey } from '../utils/alertData';
import {
  formatActiveSince,
  formatAlertAltitude,
  formatAlertEta,
  formatEpisodeDuration,
  isAlertActive,
  normalizeAlertRule,
} from '../utils/format';
import { AlertColoredLabel } from './LevelBadge';

interface AlertTimelineProps {
  alerts: Alert[];
  activeAlertId: string | null;
  zones?: Zone[];
  alertColors?: Record<string, string>;
  onSelectAlert: (alert: Alert, episodeKey: string) => void;
}

export function AlertTimeline({ alerts, activeAlertId, zones, alertColors, onSelectAlert }: AlertTimelineProps) {
  if (alerts.length === 0) {
    return <div className="alert-timeline-empty">No alert episodes for this flight.</div>;
  }

  return (
    <div id="alert-timeline-list">
      {alerts.map((alert, index) => {
        const episodeKey = alertEpisodeKey(alert, index);
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
        const title = `${isEpisodeActive ? 'Active' : 'Ended'} episode · ${durationStr}\nActivated: ${alert.activated_at ? new Date(alert.activated_at * 1000).toLocaleString() : 'N/A'}\nPosition: ${latVal}, ${lonVal}\nAltitude: ${formatAlertAltitude(alert.altitude)}${isEpisodeActive ? `\nETA: ${formatAlertEta(alert.eta)}` : ''}`;

        return (
          <button
            type="button"
            key={`${episodeKey}:${index}`}
            data-alert-episode-key={episodeKey}
            className={`alert-timeline-item ${normLevel}${isEpisodeActive ? ' episode-active' : ' episode-ended'}${episodeKey === activeAlertId ? ' active' : ''}`}
            title={title}
            onClick={() => onSelectAlert(alert, episodeKey)}
            aria-pressed={episodeKey === activeAlertId}
          >
            <div className="alert-timeline-row">
              <AlertColoredLabel
                zone={alert.zone || 'zone'}
                rule={alert.rule}
                zones={zones}
                alertColors={alertColors}
                active={isEpisodeActive}
                className="alert-timeline-zone-name"
              />
              <span className="alert-timeline-time">{timeLabel}</span>
            </div>
            <div className="alert-timeline-meta">
              <span><strong>Duration:</strong> {durationStr}</span>
              <span><strong>Alt:</strong> {formatAlertAltitude(alert.altitude)}</span>
              {isEpisodeActive && (
                <span><strong>ETA:</strong> {formatAlertEta(alert.eta)}</span>
              )}
            </div>
          </button>
        );
      })}
    </div>
  );
}
