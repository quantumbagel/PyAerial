// Timezone is pinned to UTC via vitest.config.ts so date/time assertions are deterministic.
import { describe, expect, it } from 'vitest';
import type { FlightSummary } from '../api/types';
import { flightTimeLabel } from './flightData';
import {
  formatActiveSince,
  formatDate,
  formatDateTime,
  formatTime,
  isSameLocalDay,
} from './format';

// 2023-11-14 22:13:20 UTC
const BASE_TS = 1700000000;

function flight(overrides: Partial<FlightSummary>): FlightSummary {
  return {
    flight_id: 'f1',
    icao: 'A1B2C3',
    callsign: null,
    model: null,
    aircraft_type: null,
    owner: null,
    country: null,
    start_time: BASE_TS,
    end_time: BASE_TS + 3600,
    timestamp: BASE_TS + 3600,
    is_live: false,
    active_alerts: [],
    ...overrides,
  };
}

describe('formatDate / formatTime / formatDateTime', () => {
  it('formats date only', () => {
    expect(formatDate(BASE_TS)).toBe('Nov 14, 2023');
  });

  it('formats time only', () => {
    expect(formatTime(BASE_TS)).toBe('10:13 PM');
    expect(formatTime(BASE_TS, { withSeconds: true })).toBe('10:13:20 PM');
  });

  it('formats date and time together', () => {
    expect(formatDateTime(BASE_TS)).toBe('Nov 14, 2023, 10:13 PM');
    expect(formatDateTime(BASE_TS, { withSeconds: true })).toBe('Nov 14, 2023, 10:13:20 PM');
  });

  it('returns empty string for missing or invalid timestamps', () => {
    expect(formatDate(undefined)).toBe('');
    expect(formatDate(null)).toBe('');
    expect(formatTime(0)).toBe('');
    expect(formatDateTime(Number.NaN)).toBe('');
  });
});

describe('formatActiveSince', () => {
  it('includes the date as well as the time', () => {
    expect(formatActiveSince(BASE_TS)).toBe('Nov 14, 2023, 10:13:20 PM');
  });

  it('returns N/A for missing timestamps', () => {
    expect(formatActiveSince(undefined)).toBe('N/A');
    expect(formatActiveSince(0)).toBe('N/A');
  });
});

describe('isSameLocalDay', () => {
  it('returns true for timestamps on the same local day', () => {
    expect(isSameLocalDay(BASE_TS, BASE_TS + 3600)).toBe(true);
  });

  it('returns false for timestamps on different local days', () => {
    expect(isSameLocalDay(BASE_TS, BASE_TS + 86400)).toBe(false);
  });

  it('returns false when either timestamp is missing', () => {
    expect(isSameLocalDay(undefined, BASE_TS)).toBe(false);
    expect(isSameLocalDay(BASE_TS, null)).toBe(false);
  });
});

describe('flightTimeLabel', () => {
  it('shows the date for a live flight', () => {
    const f = flight({ is_live: true, timestamp: BASE_TS, end_time: BASE_TS });
    expect(flightTimeLabel(f)).toBe('Nov 14, 2023, 10:13 PM');
  });

  it('shows the date once for an ended flight within a single day', () => {
    const f = flight({ start_time: BASE_TS, end_time: BASE_TS + 3600 });
    expect(flightTimeLabel(f)).toBe('Nov 14, 2023 · 10:13 PM – 11:13 PM');
  });

  it('shows full date-time for an ended flight spanning multiple days', () => {
    const f = flight({ start_time: BASE_TS, end_time: BASE_TS + 86400 });
    expect(flightTimeLabel(f)).toBe('Nov 14, 2023, 10:13 PM : Nov 15, 2023, 10:13 PM');
  });

  it('shows the date for the first-seen sort field', () => {
    const f = flight({ start_time: BASE_TS, end_time: BASE_TS + 3600 });
    expect(flightTimeLabel(f, 'first_seen')).toBe('Nov 14, 2023, 10:13 PM');
  });

  it('returns empty string when no timestamps are present', () => {
    const f = flight({ start_time: 0, end_time: 0, timestamp: 0, is_live: false });
    expect(flightTimeLabel(f)).toBe('');
  });
});
