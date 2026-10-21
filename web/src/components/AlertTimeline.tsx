import type { CSSProperties } from 'react';
import type { Alert, Zone } from '../api/types';
import {
  alertEpisodeKey,
  alertTooltip,
  alertVisual,
  formatEpisodeSpan,
  isAlertActive,
} from '../utils/alertData';
import { formatAltitude, formatEta } from '../utils/format';
import { ZoneBadge } from './AlertBadges';
import { StateView } from './ui';

interface AlertTimelineProps {
  alerts: Alert[];
  activeAlertId: string | null;
  zones?: Zone[];
  alertColors?: Record<string, string>;
  onSelectAlert: (alert: Alert, episodeKey: string) => void;
}

export function AlertTimeline({ alerts, activeAlertId, zones, alertColors, onSelectAlert }: AlertTimelineProps) {
  if (alerts.length === 0) {
    return <StateView layout="inline">No alert episodes for this flight.</StateView>;
  }

  return (
    <div className="ui-display-list alert-timeline">
      {alerts.map((alert) => {
        const episodeKey = alertEpisodeKey(alert);
        const isEpisodeActive = isAlertActive(alert);
        const selected = episodeKey === activeAlertId;
        const { color } = alertVisual(alert, zones, alertColors);
        return (
          <button
            type="button"
            key={episodeKey}
            data-alert-episode-key={episodeKey}
            className={`ui-display${!isEpisodeActive ? ' ui-display--muted' : ''}${selected ? ' is-active' : ''}`}
            style={{ '--tone': color } as CSSProperties}
            title={alertTooltip(alert)}
            onClick={() => onSelectAlert(alert, episodeKey)}
            aria-current={selected ? 'true' : undefined}
          >
            <div className="ui-row__line">
              <ZoneBadge
                zone={alert.zone}
                rule={alert.rule}
                active={isEpisodeActive}
                zones={zones}
                alertColors={alertColors}
              />
              <span className="ui-row__trailing">{formatEpisodeSpan(alert)}</span>
            </div>
            <div className="ui-display__meta">
              <span>
                <strong>Altitude:</strong> {formatAltitude(alert.altitude)}
              </span>
              {isEpisodeActive && (
                <span>
                  <strong>ETA:</strong> {formatEta(alert.eta)}
                </span>
              )}
            </div>
          </button>
        );
      })}
    </div>
  );
}
