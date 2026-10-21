// Timezone is pinned to UTC via vitest.config.ts; the locale is pinned here.
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { FlightSummary } from '../api/types';
import { flightTimeLabel } from './flightData';
import {
  EMPTY,
  cleanField,
  displayCallsign,
  formatAgo,
  formatAltitude,
  formatAltitudeCompact,
  formatCoordinate,
  formatDate,
  formatDateTime,
  formatDuration,
  formatEta,
  formatHeading,
  formatSpeed,
  formatTime,
  formatTimeRange,
  formatZoneRule,
  isSameLocalDay,
  safeHttpsUrl,
  setFormatLocale,
} from './format';

// 2023-11-14 22:13:20 UTC
const BASE_TS = 1700000000;

beforeAll(() => setFormatLocale('en-US'));
afterAll(() => setFormatLocale(undefined));

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

describe('dates and times', () => {
  it('formats date, time, and both', () => {
    expect(formatDate(BASE_TS)).toBe('Nov 14, 2023');
    expect(formatTime(BASE_TS)).toBe('10:13 PM');
    expect(formatTime(BASE_TS, { withSeconds: true })).toBe('10:13:20 PM');
    expect(formatDateTime(BASE_TS)).toBe('Nov 14, 2023, 10:13 PM');
    expect(formatDateTime(BASE_TS, { withSeconds: true })).toBe('Nov 14, 2023, 10:13:20 PM');
  });

  it('uses the shared placeholder for missing or invalid timestamps', () => {
    expect(formatDate(undefined)).toBe(EMPTY);
    expect(formatDate(null)).toBe(EMPTY);
    expect(formatTime(0)).toBe(EMPTY);
    expect(formatDateTime(Number.NaN)).toBe(EMPTY);
  });

  it('formats ranges with one separator', () => {
    expect(formatTimeRange(BASE_TS, BASE_TS + 3600)).toBe('Nov 14, 2023, 10:13 PM – 11:13 PM');
    expect(formatTimeRange(BASE_TS, BASE_TS + 86400)).toBe(
      'Nov 14, 2023, 10:13 PM – Nov 15, 2023, 10:13 PM',
    );
    expect(formatTimeRange(BASE_TS, null)).toBe('Nov 14, 2023, 10:13 PM');
    expect(formatTimeRange(null, null)).toBe(EMPTY);
  });

  it('follows the configured locale', () => {
    setFormatLocale('de-DE');
    expect(formatAltitudeCompact(12345)).toBe('12.345 m');
    setFormatLocale('en-US');
  });
});

describe('isSameLocalDay', () => {
  it('compares local calendar days', () => {
    expect(isSameLocalDay(BASE_TS, BASE_TS + 3600)).toBe(true);
    expect(isSameLocalDay(BASE_TS, BASE_TS + 86400)).toBe(false);
    expect(isSameLocalDay(undefined, BASE_TS)).toBe(false);
  });
});

describe('kinematics', () => {
  it('formats altitude with thousands separators in both forms', () => {
    expect(formatAltitude(1234)).toBe('1,234 m (4,049 ft)');
    expect(formatAltitudeCompact(1234.4)).toBe('1,234 m');
    expect(formatAltitude(null)).toBe(EMPTY);
    expect(formatAltitudeCompact('')).toBe(EMPTY);
  });

  it('formats speed, heading, and coordinates', () => {
    expect(formatSpeed(320)).toBe('320 km/h (173 kt)');
    expect(formatHeading(89.6)).toBe('90°');
    expect(formatCoordinate(35.7)).toBe('35.70000');
    expect(formatCoordinate(undefined)).toBe(EMPTY);
  });
});

describe('durations', () => {
  it('uses compact units', () => {
    expect(formatDuration(45)).toBe('45s');
    expect(formatDuration(200)).toBe('3m 20s');
    expect(formatDuration(3600)).toBe('1h');
    expect(formatDuration(7500)).toBe('2h 5m');
    expect(formatDuration(0)).toBe('0s');
    expect(formatDuration(null)).toBe(EMPTY);
  });

  it('formats ETA and elapsed time the same way', () => {
    expect(formatEta(45.4)).toBe('45s');
    expect(formatEta(null)).toBe(EMPTY);
    expect(formatAgo(BASE_TS - 12, BASE_TS)).toBe('12s ago');
  });
});

describe('identity fields', () => {
  it('treats placeholder strings as missing', () => {
    expect(cleanField('Unknown Model')).toBeNull();
    expect(cleanField('UNKNOWN')).toBeNull();
    expect(cleanField('  ')).toBeNull();
    expect(cleanField(null)).toBeNull();
    expect(cleanField(' Cessna ')).toBe('Cessna');
  });

  it('falls back from callsign to ICAO to the placeholder', () => {
    expect(displayCallsign({ callsign: 'N123 ', icao: 'abc' })).toBe('N123');
    expect(displayCallsign({ callsign: 'Unknown', icao: 'abc' })).toBe('ABC');
    expect(displayCallsign({ callsign: null, icao: null })).toBe(EMPTY);
  });

  it('labels zone/rule pairs', () => {
    expect(formatZoneRule('RDU', 'warn')).toBe('RDU · warn');
    expect(formatZoneRule('RDU', null)).toBe('RDU');
  });
});

describe('safeHttpsUrl', () => {
  it('allows only https links', () => {
    expect(safeHttpsUrl('https://cdn.example/p.jpg')).toBe('https://cdn.example/p.jpg');
    expect(safeHttpsUrl('javascript:alert(1)')).toBeNull();
    expect(safeHttpsUrl('http://example.com')).toBeNull();
    expect(safeHttpsUrl('not a url')).toBeNull();
  });
});

describe('flightTimeLabel', () => {
  it('shows the last fix for a live flight', () => {
    const f = flight({ is_live: true, timestamp: BASE_TS, end_time: BASE_TS });
    expect(flightTimeLabel(f)).toBe('Nov 14, 2023, 10:13 PM');
  });

  it('shows the span for an ended flight', () => {
    expect(flightTimeLabel(flight({}))).toBe('Nov 14, 2023, 10:13 PM – 11:13 PM');
    expect(flightTimeLabel(flight({ end_time: BASE_TS + 86400 }))).toBe(
      'Nov 14, 2023, 10:13 PM – Nov 15, 2023, 10:13 PM',
    );
  });

  it('shows first seen for the first-seen sort', () => {
    expect(flightTimeLabel(flight({}), 'first_seen')).toBe('Nov 14, 2023, 10:13 PM');
  });

  it('uses the placeholder when no timestamps are present', () => {
    const f = flight({ start_time: 0, end_time: 0, timestamp: 0 });
    expect(flightTimeLabel(f)).toBe(EMPTY);
  });
});
