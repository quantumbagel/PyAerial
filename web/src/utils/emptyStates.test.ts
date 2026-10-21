import { describe, expect, it } from 'vitest';
import type { ServerStats } from '../api/types';
import {
  BUSY_BANNER,
  ENGINE_IDLE_BANNER,
  ENGINE_STALE_SECONDS,
  REDIS_DOWN,
  statusBanner,
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
    history: true,
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

  it('trusts the server-computed engine_live flag over the client clock', () => {
    expect(isEngineLive(stats({ engine_live: true, engine_seen_at: NOW - 3600 }), NOW)).toBe(true);
    expect(isEngineLive(stats({ engine_live: false, engine_seen_at: NOW }), NOW)).toBe(false);
  });

  it('is true when heartbeat is recent', () => {
    expect(isEngineLive(stats({ engine_seen_at: NOW - 1 }), NOW)).toBe(true);
  });
});

describe('emptyFlightsMessage', () => {
  it('explains filters, archive, redis, and a missing engine', () => {
    expect(
      emptyFlightsMessage({ view: 'live', hasFilters: true, stats: stats() }),
    ).toMatch(/filters/);
    expect(
      emptyFlightsMessage({
        view: 'history',
        hasFilters: false,
        stats: stats({ history: false }),
      }),
    ).toMatch(/archive/);
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
    ).toBe('No aircraft in range right now.');
  });

  it('does not claim the engine is down before stats arrive', () => {
    expect(emptyFlightsMessage({ view: 'live', hasFilters: false, stats: null })).toBe(
      'No aircraft in range right now.',
    );
  });
});

describe('emptyAlertsMessage', () => {
  it('matches the live / history split', () => {
    expect(
      emptyAlertsMessage({ view: 'history', hasFilters: false, stats: stats() }),
    ).toMatch(/archived alerts/);
    expect(
      emptyAlertsMessage({ view: 'live', hasFilters: false, stats: stats() }),
    ).toBe('No alerts yet.');
  });

  it('never shows markdown backticks to users', () => {
    for (const view of ['live', 'history'] as const) {
      for (const s of [stats(), stats({ engine_seen_at: null, engine_live: false }), null]) {
        expect(emptyAlertsMessage({ view, hasFilters: false, stats: s })).not.toContain('`');
        expect(emptyFlightsMessage({ view, hasFilters: false, stats: s })).not.toContain('`');
      }
    }
  });
});

describe('statusBanner', () => {
  const base = { wsStatus: 'connected' as const, view: 'live' as const };

  it('agrees with the empty state when the live store is down', () => {
    const down = stats({ redis: false, engine_live: false });
    expect(statusBanner({ ...base, stats: down })?.text).toBe(REDIS_DOWN);
    expect(emptyFlightsMessage({ view: 'live', hasFilters: false, stats: down })).toBe(REDIS_DOWN);
  });

  it('reports an idle engine only when the store is reachable', () => {
    const idle = stats({ engine_live: false });
    expect(statusBanner({ ...base, stats: idle })?.text).toBe(ENGINE_IDLE_BANNER);
    expect(emptyFlightsMessage({ view: 'live', hasFilters: false, stats: idle })).toMatch(/pyaerial run/);
  });

  it('puts connection problems first, and is quiet when healthy', () => {
    expect(
      statusBanner({ ...base, wsStatus: 'rejected', stats: null, rejectedMessage: 'nope' }),
    ).toEqual({ text: 'nope', pending: false });
    expect(statusBanner({ ...base, wsStatus: 'busy', stats: stats({ redis: false }) })?.text).toBe(
      BUSY_BANNER,
    );
    expect(statusBanner({ ...base, stats: stats({ engine_live: true }) })).toBeNull();
    expect(statusBanner({ ...base, view: 'history', stats: stats({ engine_live: false }) })).toBeNull();
  });
});
