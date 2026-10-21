import { memo } from 'react';
import type { FlightSummary, Zone } from '../api/types';
import { flightSortValueLabel, type FlightSortField } from '../utils/flightData';
import { cleanField, displayCallsign, displayField, displayIcao } from '../utils/format';
import { FlightAlertBadges } from './AlertBadges';
import { Chip } from './ui';

interface FlightListItemProps {
  flight: FlightSummary;
  active: boolean;
  sortField: FlightSortField;
  zones?: Zone[];
  alertColors?: Record<string, string>;
  onSelect: (flightId: string) => void;
}

export const FlightListItem = memo(function FlightListItem({
  flight,
  active,
  sortField,
  zones,
  alertColors,
  onSelect,
}: FlightListItemProps) {
  const title = displayCallsign(flight);
  const icao = displayIcao(flight.icao);
  return (
    <li>
      <button
        type="button"
        className={`ui-row${active ? ' is-active' : ''}`}
        onClick={() => onSelect(flight.flight_id)}
        aria-current={active ? 'true' : undefined}
      >
        <div className="ui-row__line">
          <span className="ui-row__title">
            {title}
            <FlightAlertBadges flight={flight} zones={zones} alertColors={alertColors} />
          </span>
          {cleanField(flight.callsign) ? <Chip>{icao}</Chip> : null}
        </div>
        <div className="ui-row__line">
          <span className="ui-row__secondary">{displayField(flight.model)}</span>
          <span className="ui-row__trailing">{flightSortValueLabel(flight, sortField)}</span>
        </div>
      </button>
    </li>
  );
});
