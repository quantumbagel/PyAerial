import { describe, expect, it } from 'vitest';
import type { Alert, TelemetryPoint } from '../api/types';
import { buildAlertPathSegments, normalizeAlertEpisodes } from './alertPathSegments';

describe('normalizeAlertEpisodes', () => {
  it('merges activate/deactivate records for the same alert_id', () => {
    const merged = normalizeAlertEpisodes([
      {
        alert_id: 'f1:zone:warn',
        activated_at: 100,
        deactivated_at: null,
        active: true,
        rule: 'warn',
        zone: 'zone',
      },
      {
        alert_id: 'f1:zone:warn',
        activated_at: 100,
        deactivated_at: 200,
        active: false,
        rule: 'warn',
        zone: 'zone',
      },
    ]);
    expect(merged).toHaveLength(1);
    expect(merged[0].activated_at).toBe(100);
    expect(merged[0].deactivated_at).toBe(200);
  });
});

describe('buildAlertPathSegments', () => {
  const telemetry: TelemetryPoint[] = [
    { timestamp: 0, latitude: 35.0, longitude: -78.0 },
    { timestamp: 10, latitude: 35.01, longitude: -78.01 },
    { timestamp: 20, latitude: 35.02, longitude: -78.02 },
    { timestamp: 30, latitude: 35.03, longitude: -78.03 },
  ];

  it('returns colored segments only while an episode is active', () => {
    const alerts: Alert[] = [
      {
        alert_id: 'f1:zone:warn',
        activated_at: 5,
        deactivated_at: 25,
        rule: 'warn',
        zone: 'zone',
      },
    ];
    const segments = buildAlertPathSegments(telemetry, alerts, 30);
    expect(segments).toHaveLength(1);
    expect(segments[0].severity).toBe('warn');
    expect(segments[0].latlngs).toHaveLength(4);
  });

  it('extends active episodes through flight end when still active', () => {
    const alerts: Alert[] = [
      {
        alert_id: 'f1:zone:alert',
        activated_at: 15,
        deactivated_at: null,
        active: true,
        rule: 'alert',
        zone: 'zone',
      },
    ];
    const segments = buildAlertPathSegments(telemetry, alerts, 30);
    expect(segments).toHaveLength(1);
    expect(segments[0].severity).toBe('alert');
    expect(segments[0].latlngs[segments[0].latlngs.length - 1]).toEqual([35.03, -78.03]);
  });
});
