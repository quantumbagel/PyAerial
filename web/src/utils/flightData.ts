import type { FlightSummary, TelemetryPoint } from '../api/types';
import { flightAlertLabel } from './alertData';
import {
  EMPTY,
  formatAltitudeCompact,
  formatDateTime,
  formatNumber,
  formatTimeRange,
  isFlightLive,
} from './format';
import { sortBy, sortValueLabel, type SortDirection, type SortField } from './sort';

export type FlightSortField = 'last_seen' | 'callsign' | 'altitude' | 'integrity' | 'alerts';

function getFlightLastSeen(flight: FlightSummary): number | null {
  const ts = flight.timestamp ?? flight.end_time ?? flight.start_time;
  return ts ? ts : null;
}

function activeAlertCount(flight: FlightSummary): number {
  if (flight.active_alerts && flight.active_alerts.length > 0) {
    return flight.active_alerts.length;
  }
  // Completed flights always carry an (empty) active_alerts array, so alert
  // episode counts for history must come from alert_stats.
  return flight.alert_stats?.episode_count ?? 0;
}

function flightIdentity(flight: FlightSummary): string | null {
  const value = (flight.callsign || flight.icao || '').trim();
  return value || null;
}

/** When the flight was seen: last fix for live flights, the full span otherwise. */
export function flightTimeLabel(flight: FlightSummary): string {
  if (isFlightLive(flight)) {
    return formatDateTime(flight.timestamp ?? flight.end_time ?? flight.start_time);
  }
  return formatTimeRange(flight.start_time, flight.end_time);
}

export const FLIGHT_SORT_FIELDS: readonly SortField<FlightSummary, FlightSortField>[] = [
  {
    id: 'last_seen',
    label: 'Last Seen',
    defaultDirection: 'desc',
    value: getFlightLastSeen,
    display: flightTimeLabel,
  },
  {
    id: 'callsign',
    label: 'Callsign',
    defaultDirection: 'asc',
    value: flightIdentity,
    display: flightTimeLabel,
  },
  {
    id: 'altitude',
    label: 'Altitude',
    defaultDirection: 'desc',
    value: (flight) => flight.altitude,
    display: (flight) => formatAltitudeCompact(flight.altitude),
  },
  {
    id: 'integrity',
    label: 'Integrity',
    defaultDirection: 'desc',
    value: (flight) => flight.integrity?.score,
    display: (flight) => (flight.integrity ? formatNumber(flight.integrity.score, 2) : EMPTY),
  },
  {
    id: 'alerts',
    label: 'Alerts',
    defaultDirection: 'desc',
    value: activeAlertCount,
    display: flightAlertLabel,
  },
];

export function sortFlightsBy(
  flights: FlightSummary[],
  field: FlightSortField,
  direction: SortDirection,
): FlightSummary[] {
  return sortBy(flights, FLIGHT_SORT_FIELDS, field, direction);
}

/** Trailing text in a flight row for the active sort field. */
export function flightSortValueLabel(flight: FlightSummary, sortField: FlightSortField): string {
  return sortValueLabel(FLIGHT_SORT_FIELDS, sortField, flight, flightTimeLabel(flight));
}

function flightKinematicTime(flight: FlightSummary): number {
  return flight.timestamp ?? flight.end_time ?? 0;
}

export function mergeLiveFlights(existing: FlightSummary[], incoming: FlightSummary[]): FlightSummary[] {
  const byId = new Map(existing.map((flight) => [flight.flight_id, flight]));
  // The live snapshot is authoritative for membership: flights it omits are gone.
  return incoming.map((newFlight) => {
    const current = byId.get(newFlight.flight_id);
    if (!current || !isFlightLive(current)) return newFlight;
    const useIncomingKinematics = flightKinematicTime(newFlight) >= flightKinematicTime(current);
    return {
      ...current,
      callsign: newFlight.callsign ?? current.callsign,
      model: newFlight.model ?? current.model,
      aircraft_type: newFlight.aircraft_type ?? current.aircraft_type,
      owner: newFlight.owner ?? current.owner,
      country: newFlight.country ?? current.country,
      active_alerts: newFlight.active_alerts,
      alert_stats: newFlight.alert_stats,
      is_live: newFlight.is_live ?? current.is_live,
      ...(useIncomingKinematics
        ? {
            latitude: newFlight.latitude ?? current.latitude,
            longitude: newFlight.longitude ?? current.longitude,
            altitude: newFlight.altitude ?? current.altitude,
            speed: newFlight.speed ?? current.speed,
            heading: newFlight.heading ?? current.heading,
            timestamp: newFlight.timestamp ?? current.timestamp,
            end_time: newFlight.end_time ?? current.end_time,
          }
        : {}),
    };
  });
}

function withTelemetryPoint(flight: FlightSummary | undefined, point: TelemetryPoint): FlightSummary | null {
  if (!flight) {
    if (point.latitude == null || point.longitude == null) return null;
    return {
      flight_id: point.flight_id!,
      icao: point.icao || '',
      callsign: null,
      model: null,
      aircraft_type: null,
      owner: null,
      country: null,
      active_alerts: point.active_alerts || [],
      start_time: point.timestamp,
      end_time: point.timestamp,
      timestamp: point.timestamp,
      latitude: point.latitude,
      longitude: point.longitude,
      heading: point.heading,
      altitude: point.altitude,
      speed: point.speed,
      is_live: true,
    };
  }
  const pointTime = point.timestamp ?? 0;
  if (pointTime < flightKinematicTime(flight)) return null;
  return {
    ...flight,
    latitude: point.latitude ?? flight.latitude,
    longitude: point.longitude ?? flight.longitude,
    altitude: point.altitude ?? flight.altitude,
    speed: point.speed ?? flight.speed,
    heading: point.heading ?? flight.heading,
    timestamp: point.timestamp ?? flight.timestamp,
    end_time: point.timestamp ?? flight.end_time,
    active_alerts: point.active_alerts ?? flight.active_alerts,
    is_live: true,
  };
}

/** Apply a batch of live telemetry points; copies the list at most once. */
export function applyTelemetryBatch(
  flights: FlightSummary[],
  points: TelemetryPoint[],
): FlightSummary[] {
  let next: FlightSummary[] | null = null;
  let index: Map<string, number> | null = null;
  for (const point of points) {
    const flightId = point.flight_id;
    if (!flightId) continue;
    const list: FlightSummary[] = next ?? flights;
    index = index ?? new Map(list.map((f, i) => [f.flight_id, i]));
    const idx = index.get(flightId);
    const updated = withTelemetryPoint(idx != null ? list[idx] : undefined, point);
    if (!updated) continue;
    next = next ?? flights.slice();
    if (idx != null) {
      next[idx] = updated;
    } else {
      index.set(flightId, next.length);
      next.push(updated);
    }
  }
  return next ?? flights;
}

export function applyTelemetryPoint(
  flights: FlightSummary[],
  point: TelemetryPoint,
): FlightSummary[] {
  return applyTelemetryBatch(flights, [point]);
}
