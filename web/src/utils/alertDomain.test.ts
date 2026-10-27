import { describe, expect, it } from 'vitest';
import type { Alert, Zone } from '../api/types';
import { ApiError, apiErrorFromResponse, describeError } from '../api/errors';
import { externalTrackerLinks } from '../components/ExternalLinks';
import {
  alertSortValueLabel,
  alertVisual,
  isAlertActive,
  mergeAlertsByEpisode,
  mergeEpisode,
  primaryAlert,
} from './alertData';
import { buildAlertPathSegments } from './alertPathSegments';
import { pathStyleForFlight, planeStyle } from './planeIcon';

const zones: Zone[] = [
  {
    name: 'RDU',
    color: '#14b8a6',
    coordinates: [],
    rules: [{ name: 'warn', color: '#ff00aa', when: {}, dwell_seconds: 0 }],
  },
];

describe('episode state', () => {
  it('has one definition of active', () => {
    expect(isAlertActive({})).toBe(true);
    expect(isAlertActive({ active: true })).toBe(true);
    expect(isAlertActive({ active: false })).toBe(false);
    expect(isAlertActive({ deactivated_at: 10 })).toBe(false);
    expect(isAlertActive({ deactivated_at: null })).toBe(true);
  });

  it('treats ended as terminal when merging snapshots', () => {
    const ended: Alert = { alert_id: 'a', active: false, activated_at: 5, deactivated_at: 9 };
    const stale: Alert = { alert_id: 'a', active: true, activated_at: 5 };
    const merged = mergeEpisode(ended, stale);
    expect(merged.active).toBe(false);
    expect(merged.deactivated_at).toBe(9);
  });

  it('keeps object identity for unchanged episodes', () => {
    const a = { alert_id: 'a', zone: 'RDU', rule: 'warn', active: true, activated_at: 1 };
    const existing = [a];
    const merged = mergeAlertsByEpisode(existing, [{ ...a }]);
    expect(merged[0]).toBe(a);
  });
});

describe('alertVisual', () => {
  it('uses the configured rule colour, then alert_colors, then the zone colour', () => {
    expect(alertVisual({ zone: 'RDU', rule: 'warn' }, zones).color).toBe('#ff00aa');
    expect(alertVisual({ zone: 'RDU', rule: 'other' }, zones, { other: '#123456' }).color).toBe('#123456');
    expect(alertVisual({ zone: 'RDU', rule: 'other' }, zones).color).toBe('#14b8a6');
  });

  it('drives plane and path colours instead of rule-name keywords', () => {
    const flight = { is_live: true, active_alerts: [{ alert_id: 'x', zone: 'RDU', rule: 'warn' }] };
    expect(planeStyle(flight, false, zones).fill).toBe('#ff00aa');
    expect(pathStyleForFlight(flight, false, zones).color).toBe('#ff00aa');
  });

  it('ranks concurrent alerts by severity', () => {
    expect(primaryAlert([{ rule: 'info' }, { rule: 'critical' }, { rule: 'warn' }])?.rule).toBe('critical');
  });
});

describe('alert path segments', () => {
  it('colours stretches flown during an episode', () => {
    const telemetry = [0, 10, 20, 30].map((t) => ({ timestamp: t, latitude: t, longitude: t }));
    const segments = buildAlertPathSegments(
      telemetry,
      [{ alert_id: 'x', zone: 'RDU', rule: 'warn', activated_at: 10, deactivated_at: 20, active: false }],
      30,
    );
    expect(segments).toHaveLength(1);
    expect(segments[0].latlngs).toEqual([
      [10, 10],
      [20, 20],
    ]);
  });
});

describe('alert sort labels', () => {
  it("doesn't repeat the zone badge in the trailing label", () => {
    const label = alertSortValueLabel({ alert_id: 'a', zone: 'RDU', rule: 'warn', activated_at: 0 }, 'zone');
    expect(label).not.toContain('RDU');
  });
});

describe('external links', () => {
  it('URL-encodes callsigns, registrations and ICAOs', () => {
    const links = externalTrackerLinks({ callsign: 'AB/../x?y', registration: 'N1 2' }, 'abc#1');
    expect(links.map((l) => l.href)).toEqual([
      'https://flightaware.com/live/flight/AB%2F..%2Fx%3Fy',
      'https://globe.adsbexchange.com/?icao=abc%231',
      'https://www.radarbox.com/data/registration/N1%202',
    ]);
  });
});

describe('API errors', () => {
  it('turns server codes into specific messages', () => {
    expect(describeError(apiErrorFromResponse('rate_limited', 'x'), 'fallback')).toMatch(/Too many/);
    expect(describeError(apiErrorFromResponse('history_unavailable', 'x'), 'fallback')).toMatch(/archive/);
    expect(describeError(apiErrorFromResponse('live_unavailable', 'x'), 'fallback')).toMatch(/Redis/);
    expect(describeError(apiErrorFromResponse('internal_error', 'x'), 'fallback')).toBe('fallback');
    expect(describeError(apiErrorFromResponse('weird', 'x'), 'fallback')).toBe('fallback');
    expect(describeError(new ApiError('rejected', 'no'), 'f')).toMatch(/web\.allowed_hosts/);
  });
});
