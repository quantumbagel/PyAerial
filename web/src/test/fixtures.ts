import type {
  ActiveAlert,
  Alert,
  FlightDetail,
  FlightSummary,
  TelemetryPoint,
} from '../api/types';

// Complete wire objects for tests: every key the server always sends, with
// neutral values (null where the wire allows it), then the overrides.

export function makeFlight(overrides: Partial<FlightSummary> = {}): FlightSummary {
  return {
    flight_id: 'f1',
    icao: 'abc123',
    active_alerts: [],
    start_time: 0,
    end_time: 0,
    callsign: null,
    model: null,
    owner: null,
    country: null,
    aircraft_type: null,
    registration: null,
    latitude: null,
    longitude: null,
    altitude: null,
    speed: null,
    heading: null,
    integrity: null,
    is_live: false,
    status: overrides.is_live ? 'live' : 'completed',
    retained: false,
    timestamp: null,
    ...overrides,
  };
}

export function makeFlightDetail(overrides: Partial<FlightDetail> = {}): FlightDetail {
  return {
    flight_id: 'f1',
    icao: 'abc123',
    active_alerts: [],
    start_time: 0,
    end_time: 0,
    callsign: null,
    model: null,
    owner: null,
    country: null,
    aircraft_type: null,
    registration: null,
    integrity: null,
    is_live: false,
    status: overrides.is_live ? 'live' : 'completed',
    photo_url: null,
    photo_photographer: null,
    photo_link: null,
    ...overrides,
  };
}

export function makeActiveAlert(overrides: Partial<ActiveAlert> = {}): ActiveAlert {
  return {
    alert_id: 'f1:zone:rule',
    zone: 'zone',
    rule: 'rule',
    activated_at: 0,
    eta: null,
    ...overrides,
  };
}

export function makeAlert(overrides: Partial<Alert> = {}): Alert {
  return {
    alert_id: 'f1:zone:rule',
    flight_id: 'f1',
    icao: null,
    callsign: null,
    zone: 'zone',
    rule: 'rule',
    active: true,
    activated_at: null,
    deactivated_at: null,
    ended_by: null,
    eta: null,
    altitude: null,
    latitude: null,
    longitude: null,
    ...overrides,
  };
}

export function makePoint(overrides: Partial<TelemetryPoint> = {}): TelemetryPoint {
  return {
    flight_id: 'f1',
    timestamp: 0,
    altitude: null,
    speed: null,
    heading: null,
    ...overrides,
  };
}
