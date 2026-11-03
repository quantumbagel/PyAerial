import { describe, expect, it } from 'vitest';
import type { FlightSummary } from '../api/types';
import { applyTelemetryPoint, flightSortValueLabel, mergeLiveFlights, sortFlightsBy } from './flightData';
import { makeActiveAlert, makeFlight, makePoint } from '../test/fixtures';

function flight(overrides: Partial<FlightSummary>): FlightSummary {
  return {
    ...makeFlight(),
    flight_id: 'f1',
    icao: 'A1B2C3',
    callsign: null,
    model: null,
    aircraft_type: null,
    owner: null,
    country: null,
    start_time: 100,
    end_time: 200,
    timestamp: 200,
    is_live: false,
    active_alerts: [],
    ...overrides,
  };
}

describe('sortFlightsBy alerts', () => {
  it('ranks historical flights by alert_stats.episode_count even when active_alerts is an empty array', () => {
    const withEpisodes = flight({
      flight_id: 'hist_1',
      active_alerts: [],
      alert_stats: { episode_count: 2, total_seconds: 100, active_count: 0 },
    });
    const clean = flight({ flight_id: 'hist_2', active_alerts: [] });

    const sorted = sortFlightsBy([clean, withEpisodes], 'alerts', 'desc');
    expect(sorted.map((f) => f.flight_id)).toEqual(['hist_1', 'hist_2']);

    const ascending = sortFlightsBy([withEpisodes, clean], 'alerts', 'asc');
    expect(ascending.map((f) => f.flight_id)).toEqual(['hist_2', 'hist_1']);
  });

  it('ranks live flights by active alert count', () => {
    const twoAlerts = flight({
      flight_id: 'live_2',
      is_live: true,
      active_alerts: [
        makeActiveAlert({ zone: 'z', rule: 'warn', alert_id: 'a' }),
        makeActiveAlert({ zone: 'z', rule: 'alert', alert_id: 'b' }),
      ],
    });
    const oneAlert = flight({
      flight_id: 'live_1',
      is_live: true,
      active_alerts: [makeActiveAlert({ zone: 'z', rule: 'warn', alert_id: 'c' })],
    });

    const sorted = sortFlightsBy([oneAlert, twoAlerts], 'alerts', 'desc');
    expect(sorted.map((f) => f.flight_id)).toEqual(['live_2', 'live_1']);
  });

  it('keeps original order when alert counts are equal', () => {
    const older = flight({ flight_id: 'old', start_time: 100, end_time: 200, timestamp: 200 });
    const newer = flight({ flight_id: 'new', start_time: 150, end_time: 250, timestamp: 250 });

    const sorted = sortFlightsBy([older, newer], 'alerts', 'desc');
    expect(sorted.map((f) => f.flight_id)).toEqual(['old', 'new']);
  });

  it('ranks by integrity score and puts flights without a score last', () => {
    const low = flight({ flight_id: 'low', integrity: { score: 0.2, status: 'spoofed', checks: {} } });
    const high = flight({ flight_id: 'high', integrity: { score: 0.9, status: 'ok', checks: {} } });
    const missing = flight({ flight_id: 'none' });

    const sorted = sortFlightsBy([missing, low, high], 'integrity', 'desc');
    expect(sorted.map((f) => f.flight_id)).toEqual(['high', 'low', 'none']);
  });
});

describe('applyTelemetryPoint', () => {
  it('does not wipe last-known altitude/speed/heading when the point omits them', () => {
    const existing = flight({
      flight_id: 'live_1',
      is_live: true,
      latitude: 35.7,
      longitude: -78.7,
      altitude: 400,
      speed: 180,
      heading: 90,
    });
    const next = applyTelemetryPoint([existing], makePoint({
      flight_id: 'live_1',
      timestamp: 300,
      latitude: 35.71,
      longitude: -78.71,
    }));
    expect(next[0].altitude).toBe(400);
    expect(next[0].speed).toBe(180);
    expect(next[0].heading).toBe(90);
    expect(next[0].latitude).toBe(35.71);
  });

  it('applies velocity-only points without requiring coordinates', () => {
    const existing = flight({
      flight_id: 'live_1',
      is_live: true,
      latitude: 35.7,
      longitude: -78.7,
      speed: 180,
      heading: 90,
      timestamp: 200,
    });
    const next = applyTelemetryPoint([existing], makePoint({
      flight_id: 'live_1',
      timestamp: 300,
      speed: 210,
      heading: 95,
    }));
    expect(next[0].latitude).toBe(35.7);
    expect(next[0].speed).toBe(210);
    expect(next[0].heading).toBe(95);
  });

  it('ignores older telemetry than the current summary', () => {
    const existing = flight({
      flight_id: 'live_1',
      is_live: true,
      latitude: 35.7,
      longitude: -78.7,
      speed: 180,
      timestamp: 400,
    });
    const next = applyTelemetryPoint([existing], makePoint({
      flight_id: 'live_1',
      timestamp: 300,
      speed: 10,
      latitude: 1,
      longitude: 1,
    }));
    expect(next[0].speed).toBe(180);
    expect(next[0].latitude).toBe(35.7);
  });
});

describe('mergeLiveFlights', () => {
  it('takes the integrity verdict from each new snapshot', () => {
    const base: FlightSummary = makeFlight({
      flight_id: 'f1',
      icao: 'abc123',
      is_live: true,
      timestamp: 100,
      integrity: { score: 1, status: 'ok', checks: {} },
    });
    const next: FlightSummary = {
      ...base,
      timestamp: 101,
      integrity: { score: 0.2, status: 'spoofed', checks: {} },
    };
    const [merged] = mergeLiveFlights([base], [next]);
    expect(merged.integrity?.status).toBe('spoofed');
    // A snapshot older than the kinematics we hold still updates the verdict.
    const [stale] = mergeLiveFlights([next], [{ ...base, timestamp: 50 }]);
    expect(stale.integrity?.status).toBe('ok');
    expect(stale.timestamp).toBe(101);
  });
});

describe('integrity sorting', () => {
  it('sorts unscored ("unknown") aircraft last instead of as perfectly consistent', () => {
    const unknown = flight({ flight_id: 'u', integrity: { score: 1, status: 'unknown', checks: {} } });
    const ok = flight({ flight_id: 'ok', integrity: { score: 0.95, status: 'ok', checks: {} } });
    const bad = flight({ flight_id: 'bad', integrity: { score: 0.2, status: 'spoofed', checks: {} } });
    expect(sortFlightsBy([unknown, bad, ok], 'integrity', 'desc').map((f) => f.flight_id)).toEqual([
      'ok',
      'bad',
      'u',
    ]);
    expect(sortFlightsBy([unknown, bad, ok], 'integrity', 'asc').map((f) => f.flight_id)).toEqual([
      'bad',
      'ok',
      'u',
    ]);
    expect(flightSortValueLabel(unknown, 'integrity')).toBe('—');
  });
});

describe('mergeLiveFlights metadata', () => {
  it('picks up a registration that arrives after the first snapshot', () => {
    const first = flight({ is_live: true, registration: null });
    const [merged] = mergeLiveFlights([first], [{ ...first, registration: 'N123AA' }]);
    expect(merged.registration).toBe('N123AA');
  });
});
