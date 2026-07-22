import { describe, expect, it } from 'vitest';
import type { Alert } from '../api/types';
import { alertEpisodeKey } from './alertData';

describe('alertEpisodeKey', () => {
  it('combines alert_id, activated_at, and deactivated_at for unique episode keys', () => {
    const episodeA: Alert = {
      alert_id: 'f1:aerpaw:warn',
      activated_at: 1700000000,
      deactivated_at: 1700000300,
      active: false,
    };
    const episodeB: Alert = {
      alert_id: 'f1:aerpaw:warn',
      activated_at: 1700001000,
      active: true,
      deactivated_at: null,
    };

    expect(alertEpisodeKey(episodeA)).toBe('f1:aerpaw:warn:1700000000:1700000300');
    expect(alertEpisodeKey(episodeB)).toBe('f1:aerpaw:warn:1700001000:active');
    expect(alertEpisodeKey(episodeA)).not.toBe(alertEpisodeKey(episodeB));
  });

  it('distinguishes activation and deactivation records for the same episode', () => {
    const activationRecord: Alert = {
      alert_id: 'f1:aerpaw:warn',
      activated_at: 1700000000,
      active: true,
      deactivated_at: null,
    };
    const deactivationRecord: Alert = {
      alert_id: 'f1:aerpaw:warn',
      activated_at: 1700000000,
      active: false,
      deactivated_at: 1700000300,
    };

    expect(alertEpisodeKey(activationRecord)).toBe('f1:aerpaw:warn:1700000000:active');
    expect(alertEpisodeKey(deactivationRecord)).toBe('f1:aerpaw:warn:1700000000:1700000300');
    expect(alertEpisodeKey(activationRecord)).not.toBe(alertEpisodeKey(deactivationRecord));
  });

  it('falls back to index when activated_at is missing', () => {
    const alert: Alert = { alert_id: 'f1:aerpaw:warn' };
    expect(alertEpisodeKey(alert, 2)).toBe('f1:aerpaw:warn:idx:2');
  });
});
