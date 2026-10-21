import { memo } from 'react';
import type { Alert, Zone } from '../api/types';
import { alertSortValueLabel, alertTooltip, isAlertActive, type AlertSortField } from '../utils/alertData';
import { cleanField, displayCallsign, displayIcao } from '../utils/format';
import { ZoneBadge } from './AlertBadges';
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

export const AlertListItem = memo(function AlertListItem({
  alert,
  episodeKey,
  active,
  sortField,
  zones,
  alertColors,
  onSelect,
}: AlertListItemProps) {
  const isEpisodeActive = isAlertActive(alert);
  return (
    <li>
      <button
        type="button"
        className={`ui-row${!isEpisodeActive ? ' ui-row--muted' : ''}${active ? ' is-active' : ''}`}
        title={alertTooltip(alert)}
        onClick={() => onSelect(alert, episodeKey)}
        aria-current={active ? 'true' : undefined}
      >
        <div className="ui-row__line">
          <span className="ui-row__title">{displayCallsign(alert)}</span>
          {cleanField(alert.callsign) && cleanField(alert.icao) ? (
            <Chip>{displayIcao(alert.icao)}</Chip>
          ) : null}
        </div>
        <div className="ui-row__line">
          <ZoneBadge
            zone={alert.zone}
            rule={alert.rule}
            active={isEpisodeActive}
            zones={zones}
            alertColors={alertColors}
          />
          <span className="ui-row__trailing">{alertSortValueLabel(alert, sortField)}</span>
        </div>
      </button>
    </li>
  );
});
