import { describe, expect, it } from 'vitest';
import type { BufferedRawFrame, RawFrame, ServerStats } from '../api/types';
import {
  emptyRawMessage,
  filterRawFrames,
  formatRawDf,
  formatRawRssi,
  prependRawFrames,
  rawFrameMatches,
} from './rawFrames';

function frame(overrides: Partial<RawFrame> = {}): RawFrame {
  return {
    hex: '8d406b902015a678d4d220aa4bda',
    timestamp: 1,
    receiver: 'main',
    df: 17,
    icao: '406b90',
    ...overrides,
  };
}

function buffered(id: string, overrides: Partial<RawFrame> = {}): BufferedRawFrame {
  return { id, ...frame(overrides) };
}

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

describe('prependRawFrames', () => {
  it('puts the newest wire frame first and caps the buffer', () => {
    const existing = ['old'];
    const next = prependRawFrames(existing, ['a', 'b', 'c'], 4);
    expect(next).toEqual(['c', 'b', 'a', 'old']);
  });

  it('drops the oldest existing frames when the cap is hit', () => {
    expect(prependRawFrames(['keep', 'drop'], ['new'], 2)).toEqual(['new', 'keep']);
  });
});

describe('rawFrameMatches', () => {
  it('matches icao, hex, receiver, and df', () => {
    const row = frame();
    expect(rawFrameMatches(row, '')).toBe(true);
    expect(rawFrameMatches(row, '406B90')).toBe(true);
    expect(rawFrameMatches(row, '8d406b')).toBe(true);
    expect(rawFrameMatches(row, 'main')).toBe(true);
    expect(rawFrameMatches(row, '17')).toBe(true);
    expect(rawFrameMatches(row, 'df17')).toBe(true);
    expect(rawFrameMatches(row, 'zzz')).toBe(false);
  });
});

describe('filterRawFrames', () => {
  it('filters the buffer by query', () => {
    const rows = [
      buffered('1', { icao: 'abc123' }),
      buffered('2', { icao: 'def456', hex: '8ddef45600' }),
    ];
    expect(filterRawFrames(rows, 'abc').map((r) => r.id)).toEqual(['1']);
  });
});

describe('formatters', () => {
  it('formats df and rssi', () => {
    expect(formatRawDf(17)).toBe('DF17');
    expect(formatRawDf(undefined)).toBe('DF—');
    expect(formatRawRssi(-18.5)).toBe('-18.5 dBFS');
    expect(formatRawRssi(undefined)).toBe('');
  });
});

describe('emptyRawMessage', () => {
  it('explains filters, auth, redis, and a missing engine', () => {
    expect(
      emptyRawMessage({ hasFilters: true, stats: stats(), rawStatus: 'connected' }),
    ).toMatch(/filters/);
    expect(
      emptyRawMessage({ hasFilters: false, stats: stats(), rawStatus: 'rejected' }),
    ).toMatch(/denied/);
    expect(
      emptyRawMessage({
        hasFilters: false,
        stats: stats({ redis: false }),
        rawStatus: 'connected',
      }),
    ).toMatch(/Redis/);
    expect(
      emptyRawMessage({
        hasFilters: false,
        stats: stats({ engine_seen_at: null }),
        rawStatus: 'connected',
      }),
    ).toMatch(/pyaerial run/);
    expect(
      emptyRawMessage({ hasFilters: false, stats: stats(), rawStatus: 'connected' }),
    ).toMatch(/No Beast frames yet/);
  });
});
