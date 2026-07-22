import type { FlightSummary, Zone } from '../api/types';
import type { FlightSortField } from '../utils/flightData';
import { flightSortValueLabel, flightTimeLabel, LevelBadge } from './LevelBadge';

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
        className={`list-row flight-item${active ? ' active' : ''}`}
        onClick={() => onSelect(flight.flight_id)}
        aria-pressed={active}
      >
        <div className="flight-meta-row">
          <span className="flight-callsign">
            {displayCallsign}{' '}
            <LevelBadge flight={flight} zones={zones} alertColors={alertColors} />
          </span>
          <span className="flight-icao">{flight.icao.toUpperCase()}</span>
        </div>
        <div className="flight-meta-row">
          <span className="flight-desc">{flight.model && flight.model !== 'Unknown Model' ? flight.model : '—'}</span>
          <span className="flight-time">
            {isTimeSortField ? flightTimeLabel(flight, sortField) : flightSortValueLabel(flight, sortField, alertCount)}
          </span>
        </div>
      </button>
    </li>
  );
}
