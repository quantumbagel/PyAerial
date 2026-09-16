import { describe, expect, it } from 'vitest';
import type { ServerStats } from '../api/types';
import {
  ENGINE_STALE_SECONDS,
  emptyAlertsMessage,
  emptyFlightsMessage,
  isEngineLive,
} from './emptyStates';

const NOW = 1_700_000_000;

function stats(overrides: Partial<ServerStats> = {}): ServerStats {
  return {
    live_flights: 0,
    active_alerts: 0,
    retained_flights: 0,
    historical_alerts: 0,
    redis: true,
    mongo: true,
    engine_seen_at: Date.now() / 1000 - 1,
    ...overrides,
  };
}

describe('isEngineLive', () => {
  it('is false when heartbeat is missing or stale', () => {
    expect(isEngineLive(null, NOW)).toBe(false);
    expect(isEngineLive(stats({ engine_seen_at: null }), NOW)).toBe(false);
    expect(isEngineLive(stats({ engine_seen_at: NOW - ENGINE_STALE_SECONDS }), NOW)).toBe(
      false,
    );
  });

  it('is true when heartbeat is recent', () => {
    expect(isEngineLive(stats({ engine_seen_at: NOW - 1 }), NOW)).toBe(true);
  });
});

describe('emptyFlightsMessage', () => {
  it('explains filters, mongo, redis, and a missing engine', () => {
    expect(
      emptyFlightsMessage({ view: 'live', hasFilters: true, stats: stats() }),
    ).toMatch(/filters/);
    expect(
      emptyFlightsMessage({
        view: 'history',
        hasFilters: false,
        stats: stats({ mongo: false }),
      }),
    ).toMatch(/MongoDB/);
    expect(
      emptyFlightsMessage({ view: 'history', hasFilters: false, stats: stats() }),
    ).toMatch(/retain rule/);
    expect(
      emptyFlightsMessage({
        view: 'live',
        hasFilters: false,
        stats: stats({ redis: false }),
      }),
    ).toMatch(/Redis/);
    expect(
      emptyFlightsMessage({
        view: 'live',
        hasFilters: false,
        stats: stats({ engine_seen_at: null }),
      }),
    ).toMatch(/pyaerial run/);
    expect(
      emptyFlightsMessage({ view: 'live', hasFilters: false, stats: stats() }),
    ).toBe('No aircraft on the live feed.');
  });

  it('does not claim the engine is down before stats arrive', () => {
    expect(emptyFlightsMessage({ view: 'live', hasFilters: false, stats: null })).toBe(
      'No aircraft on the live feed.',
    );
  });
});

describe('emptyAlertsMessage', () => {
  it('matches the live / history split', () => {
    expect(
      emptyAlertsMessage({ view: 'history', hasFilters: false, stats: stats() }),
    ).toMatch(/retained alerts/);
    expect(
      emptyAlertsMessage({ view: 'live', hasFilters: false, stats: stats() }),
    ).toBe('No geofence alerts yet.');
  });
});
