import type { FlightSummary, TelemetryPoint } from '../api/types';
import { isFlightLive } from './format';

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
