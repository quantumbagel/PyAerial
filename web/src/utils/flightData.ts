import type { FlightSummary, TelemetryPoint } from '../api/types';
import { readStoredJson, writeStored } from './storage';
import { flightAlertLabel } from './alertData';
import {
  cleanField,
  displayCallsign,
  displayField,
  displayIcao,
  formatAltitudeCompact,
  formatDateTime,
  formatDuration,
  formatSpeedCompact,
  formatTimeRange,
  isFlightLive,
} from './format';

export type FlightSortField =
  | 'last_seen'
  | 'first_seen'
  | 'duration'
  | 'alerts'
  | 'callsign'
  | 'icao'
  | 'model'
  | 'type'
  | 'altitude'
  | 'speed';

export type SortDirection = 'asc' | 'desc';

export const FLIGHT_SORT_OPTIONS: { value: FlightSortField; label: string }[] = [
  { value: 'last_seen', label: 'Last Seen' },
  { value: 'first_seen', label: 'First Seen' },
  { value: 'duration', label: 'Duration' },
  { value: 'alerts', label: 'Alerts' },
  { value: 'callsign', label: 'Callsign' },
  { value: 'icao', label: 'ICAO' },
  { value: 'model', label: 'Model' },
  { value: 'type', label: 'Type' },
  { value: 'altitude', label: 'Altitude' },
  { value: 'speed', label: 'Speed' },
];

export function defaultSortDirection(field: FlightSortField): SortDirection {
  if (field === 'callsign' || field === 'icao' || field === 'model' || field === 'type') {
    return 'asc';
  }
  return 'desc';
}

function getFlightLastSeen(flight: FlightSummary): number {
  return flight.timestamp ?? flight.end_time ?? flight.start_time ?? 0;
}

function getFlightDuration(flight: FlightSummary): number {
  const start = flight.start_time ?? 0;
  const end = flight.end_time ?? flight.timestamp ?? start;
  return Math.max(0, end - start);
}

function getFlightAircraftType(flight: FlightSummary): string {
  return (cleanField(flight.aircraft_type) || '').toLowerCase();
}

function isFiniteSortNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function activeAlertCount(flight: FlightSummary): number {
  if (flight.active_alerts && flight.active_alerts.length > 0) {
    return flight.active_alerts.length;
  }
  // Completed flights always carry an (empty) active_alerts array, so alert
  // episode counts for history must come from alert_stats.
  return flight.alert_stats?.episode_count ?? 0;
}

function isFlightSortValueMissing(flight: FlightSummary, field: FlightSortField): boolean {
  switch (field) {
    case 'altitude':
      return !isFiniteSortNumber(flight.altitude);
    case 'speed':
      return !isFiniteSortNumber(flight.speed);
    case 'last_seen':
      return getFlightLastSeen(flight) === 0;
    case 'first_seen':
      return (flight.start_time ?? 0) === 0;
    case 'callsign':
      return !cleanField(flight.callsign);
    case 'icao':
      return !cleanField(flight.icao);
    case 'model':
      return !cleanField(flight.model);
    case 'type':
      return !getFlightAircraftType(flight);
    case 'alerts':
      return false;
    default:
      return false;
  }
}

const textCompare = (a?: string | null, b?: string | null) =>
  (a || '').localeCompare(b || '', undefined, { sensitivity: 'base' });

function compareFlightsByField(
  a: FlightSummary,
  b: FlightSummary,
  field: FlightSortField,
): number {
  switch (field) {
    case 'alerts': {
      const aCount = activeAlertCount(a);
      const bCount = activeAlertCount(b);
      return aCount - bCount;
    }
    case 'last_seen':
      return getFlightLastSeen(a) - getFlightLastSeen(b);
    case 'first_seen':
      return (a.start_time ?? 0) - (b.start_time ?? 0);
    case 'duration':
      return getFlightDuration(a) - getFlightDuration(b);
    case 'callsign':
      return textCompare(cleanField(a.callsign), cleanField(b.callsign));
    case 'icao':
      return textCompare(a.icao, b.icao);
    case 'model':
      return textCompare(cleanField(a.model), cleanField(b.model));
    case 'type':
      return textCompare(getFlightAircraftType(a), getFlightAircraftType(b));
    case 'altitude':
      return (a.altitude as number) - (b.altitude as number);
    case 'speed':
      return (a.speed as number) - (b.speed as number);
    default:
      return 0;
  }
}

function compareFlightsByLastSeenDesc(a: FlightSummary, b: FlightSummary): number {
  return compareFlightsByField(a, b, 'last_seen') * -1;
}

export function sortFlightsBy(
  flights: FlightSummary[],
  field: FlightSortField,
  direction: SortDirection,
): FlightSummary[] {
  const mult = direction === 'asc' ? 1 : -1;
  return [...flights].sort((a, b) => {
    const aMissing = isFlightSortValueMissing(a, field);
    const bMissing = isFlightSortValueMissing(b, field);
    if (aMissing && bMissing) return compareFlightsByLastSeenDesc(a, b);
    if (aMissing) return 1;
    if (bMissing) return -1;

    const cmp = compareFlightsByField(a, b, field);
    if (cmp !== 0) return cmp * mult;
    return compareFlightsByLastSeenDesc(a, b);
  });
}

export function loadFlightSort(view: 'live' | 'history'): {
  field: FlightSortField;
  direction: SortDirection;
} {
  const parsed = readStoredJson<{ field?: FlightSortField; direction?: SortDirection }>(
    `flightSort:${view}`,
  );
  const field = FLIGHT_SORT_OPTIONS.some((o) => o.value === parsed?.field)
    ? parsed!.field!
    : 'last_seen';
  const direction =
    parsed?.direction === 'asc' || parsed?.direction === 'desc'
      ? parsed.direction
      : defaultSortDirection(field);
  return { field, direction };
}

export function saveFlightSort(
  view: 'live' | 'history',
  field: FlightSortField,
  direction: SortDirection,
): void {
  writeStored(`flightSort:${view}`, JSON.stringify({ field, direction }));
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
      callsign: newFlight.callsign || current.callsign,
      model: newFlight.model || current.model,
      aircraft_type: newFlight.aircraft_type || current.aircraft_type,
      owner: newFlight.owner || current.owner,
      country: newFlight.country || current.country,
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

/** Trailing text in a flight row for the active sort field. */
export function flightSortValueLabel(flight: FlightSummary, sortField: FlightSortField): string {
  switch (sortField) {
    case 'alerts':
      return flightAlertLabel(flight);
    case 'altitude':
      return formatAltitudeCompact(flight.altitude);
    case 'speed':
      return formatSpeedCompact(flight.speed);
    case 'callsign':
      return displayCallsign(flight);
    case 'icao':
      return displayIcao(flight.icao);
    case 'model':
      return displayField(flight.model);
    case 'type':
      return displayField(flight.aircraft_type);
    case 'duration':
      return formatDuration(getFlightDuration(flight));
    default:
      return flightTimeLabel(flight, sortField);
  }
}

/** When the flight was seen: last fix for live flights, the full span otherwise. */
export function flightTimeLabel(flight: FlightSummary, sortField?: FlightSortField): string {
  if (sortField === 'first_seen') {
    return formatDateTime(flight.start_time ?? flight.timestamp ?? flight.end_time);
  }
  if (isFlightLive(flight)) {
    return formatDateTime(flight.timestamp ?? flight.end_time ?? flight.start_time);
  }
  return formatTimeRange(flight.start_time, flight.end_time);
}

export const FLIGHT_SORT_STORE = {
  load: loadFlightSort,
  save: saveFlightSort,
  defaultDirection: defaultSortDirection,
};
