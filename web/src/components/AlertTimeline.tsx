import type { Alert, Zone } from '../api/types';
import { alertEpisodeKey } from '../utils/alertData';
import {
  formatActiveSince,
  formatAlertAltitude,
  formatAlertEta,
  formatDateTime,
  formatEpisodeDuration,
  formatZoneRule,
  isAlertActive,
  normalizeAlertRule,
} from '../utils/format';
import { ZoneBadge } from './LevelBadge';

interface AlertTimelineProps {
  alerts: Alert[];
  activeAlertId: string | null;
  zones?: Zone[];
  alertColors?: Record<string, string>;
  onSelectAlert: (alert: Alert, episodeKey: string) => void;
}

export function AlertTimeline({ alerts, activeAlertId, zones, alertColors, onSelectAlert }: AlertTimelineProps) {
  if (alerts.length === 0) {
    return <div className="ui-empty ui-empty--inline">No alert episodes for this flight.</div>;
  }

  return (
    <div id="alert-timeline-list" className="ui-display-list">
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
          ? `${activatedStr} (${durationStr})`
          : deactivatedStr
            ? `${activatedStr} – ${deactivatedStr} (${durationStr})`
            : `${activatedStr} (${durationStr})`;
        const latVal = alert.latitude != null ? alert.latitude.toFixed(5) : 'N/A';
        const lonVal = alert.longitude != null ? alert.longitude.toFixed(5) : 'N/A';
        const title = `${isEpisodeActive ? 'Active' : 'Ended'} episode · ${durationStr}\nActivated: ${formatDateTime(alert.activated_at) || 'N/A'}\nPosition: ${latVal}, ${lonVal}\nAltitude: ${formatAlertAltitude(alert.altitude)}${isEpisodeActive ? `\nETA: ${formatAlertEta(alert.eta)}` : ''}`;

        return (
          <button
            type="button"
            key={`${episodeKey}:${index}`}
            data-alert-episode-key={episodeKey}
            className={`ui-display ui-display--${normLevel}${!isEpisodeActive ? ' ui-display--muted' : ''}${episodeKey === activeAlertId ? ' is-active' : ''}`}
            title={title}
            onClick={() => onSelectAlert(alert, episodeKey)}
            aria-pressed={episodeKey === activeAlertId}
          >
            <div className="ui-row__line">
              <ZoneBadge
                zone={alert.zone || 'zone'}
                rule={alert.rule}
                zones={zones}
                alertColors={alertColors}
                label={formatZoneRule(alert.zone || 'zone', alert.rule, { live: isEpisodeActive })}
              />
              <span className="ui-row__trailing">{timeLabel}</span>
            </div>
            <div className="ui-display__meta">
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
