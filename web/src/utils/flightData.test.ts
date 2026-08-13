import { describe, expect, it } from 'vitest';
import type { FlightSummary } from '../api/types';
import { sortFlightsBy } from './flightData';

function flight(overrides: Partial<FlightSummary>): FlightSummary {
  return {
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
        { zone: 'z', rule: 'warn', alert_id: 'a' },
        { zone: 'z', rule: 'alert', alert_id: 'b' },
      ],
    });
    const oneAlert = flight({
      flight_id: 'live_1',
      is_live: true,
      active_alerts: [{ zone: 'z', rule: 'warn', alert_id: 'c' }],
    });

    const sorted = sortFlightsBy([oneAlert, twoAlerts], 'alerts', 'desc');
    expect(sorted.map((f) => f.flight_id)).toEqual(['live_2', 'live_1']);
  });

  it('ties sort by last seen when alert counts are equal', () => {
    const older = flight({ flight_id: 'old', start_time: 100, end_time: 200, timestamp: 200 });
    const newer = flight({ flight_id: 'new', start_time: 150, end_time: 250, timestamp: 250 });

    const sorted = sortFlightsBy([older, newer], 'alerts', 'desc');
    expect(sorted.map((f) => f.flight_id)).toEqual(['new', 'old']);
  });
});
