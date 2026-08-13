import { describe, expect, it } from 'vitest';
import type { Alert } from '../api/types';
import { alertEpisodeIdentity, dedupeAlerts, mergeAlertsByEpisode } from './alertData';

describe('alertEpisodeIdentity & episode deduplication', () => {
  it('produces identical identity keys when starting timestamps are identical', () => {
    const activeAlert: Alert = {
      alert_id: 'flight_1:zone_a:warn',
      flight_id: 'flight_1',
      zone: 'zone_a',
      rule: 'warn',
      active: true,
      activated_at: 1700000000,
    };

    const finishedAlert: Alert = {
      alert_id: 'flight_1:zone_a:warn',
      flight_id: 'flight_1',
      zone: 'zone_a',
      rule: 'warn',
      active: false,
      activated_at: 1700000000,
      deactivated_at: 1700000100,
    };

    expect(alertEpisodeIdentity(activeAlert)).toBe('flight_1:zone_a:warn');
    expect(alertEpisodeIdentity(finishedAlert)).toBe('flight_1:zone_a:warn');
    expect(alertEpisodeIdentity(activeAlert)).toBe(alertEpisodeIdentity(finishedAlert));
  });

  it('matches active alert missing alert_id with finished alert using flight_id:zone:rule', () => {
    const activeAlertFromFlight: Alert = {
      alert_id: '',
      flight_id: 'flight_1',
      zone: 'zone_a',
      rule: 'warn',
      active: true,
      activated_at: 1700000000,
    };

    const finishedAlert: Alert = {
      alert_id: 'flight_1:zone_a:warn',
      flight_id: 'flight_1',
      zone: 'zone_a',
      rule: 'warn',
      active: false,
      activated_at: 1700000000,
      deactivated_at: 1700000100,
    };

    expect(alertEpisodeIdentity(activeAlertFromFlight)).toBe('flight_1:zone_a:warn');
    expect(alertEpisodeIdentity(finishedAlert)).toBe('flight_1:zone_a:warn');

    const result = dedupeAlerts([activeAlertFromFlight, finishedAlert]);
    expect(result).toHaveLength(1);
    expect(result[0].active).toBe(false);
    expect(result[0].deactivated_at).toBe(1700000100);
  });

  it('deduplicates active and finished states into a single concluded alert', () => {
    const activeAlert: Alert = {
      alert_id: 'flight_1:zone_a:warn',
      flight_id: 'flight_1',
      zone: 'zone_a',
      rule: 'warn',
      active: true,
      activated_at: 1700000000,
    };

    const finishedAlert: Alert = {
      alert_id: 'flight_1:zone_a:warn',
      flight_id: 'flight_1',
      zone: 'zone_a',
      rule: 'warn',
      active: false,
      activated_at: 1700000000,
      deactivated_at: 1700000100,
    };

    const result = dedupeAlerts([activeAlert, finishedAlert]);
    expect(result).toHaveLength(1);
    expect(result[0].active).toBe(false);
    expect(result[0].deactivated_at).toBe(1700000100);
  });

  it('keeps a re-activated episode separate from a previously ended one (unique alert ids)', () => {
    const firstEpisode = {
      alert_id: 'flight_1:zone_a:warn:1700000000',
      flight_id: 'flight_1',
      zone: 'zone_a',
      rule: 'warn',
      active: false,
      activated_at: 1700000000,
      deactivated_at: 1700000100,
    };
    const reactivatedEpisode = {
      alert_id: 'flight_1:zone_a:warn:1700000300',
      flight_id: 'flight_1',
      zone: 'zone_a',
      rule: 'warn',
      active: true,
      activated_at: 1700000300,
    };

    expect(alertEpisodeIdentity(firstEpisode)).not.toBe(alertEpisodeIdentity(reactivatedEpisode));

    const result = dedupeAlerts([firstEpisode, reactivatedEpisode]);
    expect(result).toHaveLength(2);
    expect(result.find((a) => a.alert_id === reactivatedEpisode.alert_id)?.active).toBe(true);
  });

  it('merges finished alert update over existing active alert in mergeAlertsByEpisode', () => {
    const activeAlert: Alert = {
      alert_id: 'flight_1:zone_a:warn',
      flight_id: 'flight_1',
      zone: 'zone_a',
      rule: 'warn',
      active: true,
      activated_at: 1700000000,
    };

    const finishedAlert: Alert = {
      alert_id: 'flight_1:zone_a:warn',
      flight_id: 'flight_1',
      zone: 'zone_a',
      rule: 'warn',
      active: false,
      activated_at: 1700000000,
      deactivated_at: 1700000100,
    };

    const merged = mergeAlertsByEpisode([activeAlert], [finishedAlert]);
    expect(merged).toHaveLength(1);
    expect(merged[0].active).toBe(false);
    expect(merged[0].deactivated_at).toBe(1700000100);
  });
});
