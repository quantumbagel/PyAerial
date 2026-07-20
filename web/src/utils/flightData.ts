import type { FlightSummary, TelemetryPoint } from '../api/types';
import { isFlightLive } from './format';

export type FlightSortField =
  | 'last_seen'
  | 'first_seen'
  | 'duration'
  | 'callsign'
  | 'icao'
  | 'model'
  | 'type'
  | 'altitude'
  | 'speed'
  | 'zone'
  | 'level';

export type SortDirection = 'asc' | 'desc';

export const FLIGHT_SORT_OPTIONS: { value: FlightSortField; label: string }[] = [
  { value: 'last_seen', label: 'Last Seen' },
  { value: 'first_seen', label: 'First Seen' },
  { value: 'duration', label: 'Duration' },
  { value: 'callsign', label: 'Callsign' },
  { value: 'icao', label: 'ICAO' },
  { value: 'model', label: 'Model' },
  { value: 'type', label: 'Type' },
  { value: 'altitude', label: 'Altitude' },
  { value: 'speed', label: 'Speed' },
  { value: 'zone', label: 'Zone' },
  { value: 'level', label: 'Level' },
];

export function defaultSortDirection(field: FlightSortField): SortDirection {
  if (field === 'callsign' || field === 'icao' || field === 'model' || field === 'type' || field === 'zone' || field === 'level') {
    return 'asc';
  }
  return 'desc';
}

export function getFlightLastSeen(flight: FlightSummary): number {
  return flight.timestamp ?? flight.end_time ?? flight.start_time ?? 0;
}

export function getFlightDuration(flight: FlightSummary): number {
  const start = flight.start_time ?? 0;
  const end = flight.end_time ?? flight.timestamp ?? start;
  return Math.max(0, end - start);
}

function getFlightAircraftType(flight: FlightSummary): string {
  return (flight.aircraft_type || flight.typecode || '').toLowerCase();
}

function isFiniteSortNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

export function isFlightSortValueMissing(flight: FlightSummary, field: FlightSortField): boolean {
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
      return !flight.callsign?.trim();
    case 'icao':
      return !flight.icao?.trim();
    case 'model':
      return !flight.model?.trim();
    case 'type':
      return !getFlightAircraftType(flight);
    case 'zone':
      return !flight.zone?.trim();
    case 'level':
      return !flight.level?.trim();
    default:
      return false;
  }
}

function compareFlightsByField(a: FlightSummary, b: FlightSummary, field: FlightSortField): number {
  switch (field) {
    case 'last_seen':
      return getFlightLastSeen(a) - getFlightLastSeen(b);
    case 'first_seen':
      return (a.start_time ?? 0) - (b.start_time ?? 0);
    case 'duration':
      return getFlightDuration(a) - getFlightDuration(b);
    case 'callsign':
      return (a.callsign || '').localeCompare(b.callsign || '', undefined, { sensitivity: 'base' });
    case 'icao':
      return (a.icao || '').localeCompare(b.icao || '', undefined, { sensitivity: 'base' });
    case 'model':
      return (a.model || '').localeCompare(b.model || '', undefined, { sensitivity: 'base' });
    case 'type':
      return getFlightAircraftType(a).localeCompare(getFlightAircraftType(b), undefined, { sensitivity: 'base' });
    case 'altitude':
      return (a.altitude as number) - (b.altitude as number);
    case 'speed':
      return (a.speed as number) - (b.speed as number);
    case 'zone':
      return (a.zone || '').localeCompare(b.zone || '', undefined, { sensitivity: 'base' });
    case 'level':
      return (a.level || '').localeCompare(b.level || '', undefined, { sensitivity: 'base' });
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
  try {
    const raw = localStorage.getItem(`flightSort:${view}`);
    if (!raw) return { field: 'last_seen', direction: 'desc' };
    const parsed = JSON.parse(raw) as { field?: FlightSortField; direction?: SortDirection };
    const field = FLIGHT_SORT_OPTIONS.some((o) => o.value === parsed.field)
      ? parsed.field!
      : 'last_seen';
    const direction = parsed.direction === 'asc' || parsed.direction === 'desc'
      ? parsed.direction
      : defaultSortDirection(field);
    return { field, direction };
  } catch {
    return { field: 'last_seen', direction: 'desc' };
  }
}

export function saveFlightSort(
  view: 'live' | 'history',
  field: FlightSortField,
  direction: SortDirection,
): void {
  localStorage.setItem(`flightSort:${view}`, JSON.stringify({ field, direction }));
}

export function mergeLiveFlights(existing: FlightSummary[], incoming: FlightSummary[]): FlightSummary[] {
  const next = [...existing];
  incoming.forEach((newFlight) => {
    const idx = next.findIndex((f) => f.flight_id === newFlight.flight_id);
    if (idx !== -1) {
      if (isFlightLive(next[idx])) {
        next[idx] = {
          ...next[idx],
          callsign: newFlight.callsign || next[idx].callsign,
          model: newFlight.model || next[idx].model,
          aircraft_type: newFlight.aircraft_type || next[idx].aircraft_type,
          owner: newFlight.owner || next[idx].owner,
          country: newFlight.country || next[idx].country,
          zone: newFlight.zone,
          level: newFlight.level,
          latitude: newFlight.latitude ?? next[idx].latitude,
          longitude: newFlight.longitude ?? next[idx].longitude,
          altitude: newFlight.altitude ?? next[idx].altitude,
          speed: newFlight.speed ?? next[idx].speed,
          heading: newFlight.heading ?? next[idx].heading,
          timestamp: newFlight.timestamp ?? next[idx].timestamp,
          end_time: newFlight.end_time ?? next[idx].end_time,
        };
      } else {
        next[idx] = newFlight;
      }
    } else {
      next.push(newFlight);
    }
  });
  const remoteIds = new Set(incoming.map((f) => f.flight_id));
  return next.filter((f) => remoteIds.has(f.flight_id));
}

export function applyTelemetryPoint(
  flights: FlightSummary[],
  point: TelemetryPoint,
): FlightSummary[] {
  const flightId = point.flight_id;
  if (!flightId || point.latitude == null || point.longitude == null) return flights;
  const next = [...flights];
  let flight = next.find((f) => f.flight_id === flightId);
  if (!flight) {
    flight = {
      flight_id: flightId,
      icao: point.icao || '',
      callsign: null,
      model: null,
      aircraft_type: null,
      owner: null,
      country: null,
      zone: point.zone || '',
      level: point.level || '',
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
    next.push(flight);
  } else {
    const idx = next.indexOf(flight);
    next[idx] = {
      ...flight,
      latitude: point.latitude,
      longitude: point.longitude,
      altitude: point.altitude,
      speed: point.speed,
      heading: point.heading,
      timestamp: point.timestamp,
      end_time: point.timestamp,
      zone: point.zone || flight.zone,
      level: point.level || flight.level,
      is_live: true,
    };
  }
  return next;
}

export function sortFlights(flights: FlightSummary[]): FlightSummary[] {
  return [...flights].sort((a, b) => {
    const aLive = isFlightLive(a);
    const bLive = isFlightLive(b);
    if (aLive && !bLive) return -1;
    if (!aLive && bLive) return 1;
    return (b.start_time || 0) - (a.start_time || 0);
  });
}
