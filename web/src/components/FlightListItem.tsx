import type { FlightSummary, Zone } from '../api/types';
import {
  flightSortValueLabel,
  flightTimeLabel,
  type FlightSortField,
} from '../utils/flightData';
import { Chip } from './ui';
import { LevelBadge } from './LevelBadge';

interface FlightListItemProps {
  flight: FlightSummary;
  active: boolean;
  sortField: FlightSortField;
  alertCount?: number;
  zones?: Zone[];
  alertColors?: Record<string, string>;
  onSelect: (flightId: string) => void;
}

export function FlightListItem({
  flight,
  active,
  sortField,
  alertCount,
  zones,
  alertColors,
  onSelect,
}: FlightListItemProps) {
  const isTimeSortField = sortField === 'last_seen' || sortField === 'first_seen';
  const rawCallsign = flight.callsign?.trim();
  const displayCallsign =
    rawCallsign && rawCallsign.toUpperCase() !== 'UNKNOWN'
      ? rawCallsign
      : flight.icao.toUpperCase();
  return (
    <li>
      <button
        type="button"
        className={`ui-row${active ? ' is-active' : ''}`}
        onClick={() => onSelect(flight.flight_id)}
        aria-pressed={active}
      >
        <div className="ui-row__line">
          <span className="ui-row__title">
            {displayCallsign}{' '}
            <LevelBadge flight={flight} zones={zones} alertColors={alertColors} />
          </span>
          <Chip>{flight.icao.toUpperCase()}</Chip>
        </div>
        <div className="ui-row__line">
          <span className="ui-row__secondary">
            {flight.model && flight.model !== 'Unknown Model' ? flight.model : '—'}
          </span>
          <span className="ui-row__trailing">
            {isTimeSortField ? flightTimeLabel(flight, sortField) : flightSortValueLabel(flight, sortField, alertCount)}
          </span>
        </div>
      </button>
    </li>
  );
}
