import { describe, expect, it } from 'vitest';
import type { FlightSummary } from '../api/types';
import {
  applyTelemetryPoint,
  mergeLiveFlights,
  sortFlightsBy,
} from './flightData';

function flight(overrides: Partial<FlightSummary> = {}): FlightSummary {
  return {
    flight_id: 'f1',
    icao: 'abc123',
    callsign: 'TEST01',
    is_live: true,
    timestamp: 100,
    start_time: 50,
    ...overrides,
  };
}

describe('sortFlightsBy', () => {
  it('sorts by callsign ascending with missing values last', () => {
    const flights = [
      flight({ flight_id: 'a', callsign: 'ZULU' }),
      flight({ flight_id: 'b', callsign: 'ALPHA' }),
      flight({ flight_id: 'c', callsign: null }),
    ];
    const sorted = sortFlightsBy(flights, 'callsign', 'asc');
    expect(sorted.map((f) => f.flight_id)).toEqual(['b', 'a', 'c']);
  });

  it('sorts by altitude descending', () => {
    const flights = [
      flight({ flight_id: 'a', altitude: 1000 }),
      flight({ flight_id: 'b', altitude: 5000 }),
      flight({ flight_id: 'c', altitude: 2000 }),
    ];
    const sorted = sortFlightsBy(flights, 'altitude', 'desc');
    expect(sorted.map((f) => f.flight_id)).toEqual(['b', 'c', 'a']);
  });
});

describe('mergeLiveFlights', () => {
  it('preserves live telemetry when merging snapshot updates', () => {
    const existing = [flight({ latitude: 1, longitude: 2, altitude: 300 })];
    const incoming = [flight({ latitude: null, longitude: null, altitude: null, callsign: 'NEW' })];
    const merged = mergeLiveFlights(existing, incoming);
    expect(merged[0].callsign).toBe('NEW');
    expect(merged[0].latitude).toBe(1);
    expect(merged[0].longitude).toBe(2);
    expect(merged[0].altitude).toBe(300);
  });

  it('drops flights removed from the server snapshot', () => {
    const existing = [flight({ flight_id: 'gone' }), flight({ flight_id: 'stay' })];
    const incoming = [flight({ flight_id: 'stay' })];
    const merged = mergeLiveFlights(existing, incoming);
    expect(merged.map((f) => f.flight_id)).toEqual(['stay']);
  });
});

describe('applyTelemetryPoint', () => {
  it('ignores points without coordinates', () => {
    const flights = [flight()];
    const next = applyTelemetryPoint(flights, {
      timestamp: 200,
      flight_id: 'f1',
      latitude: null,
      longitude: null,
    });
    expect(next).toBe(flights);
  });

  it('updates an existing flight position', () => {
    const flights = [flight({ latitude: 1, longitude: 2 })];
    const next = applyTelemetryPoint(flights, {
      timestamp: 200,
      flight_id: 'f1',
      latitude: 3,
      longitude: 4,
      altitude: 500,
    });
    expect(next[0].latitude).toBe(3);
    expect(next[0].longitude).toBe(4);
    expect(next[0].altitude).toBe(500);
  });
});
