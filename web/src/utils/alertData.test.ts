import { describe, expect, it } from 'vitest';
import type { Alert } from '../api/types';
import { alertTooltip, endedByLabel, mergeAlertsByEpisode } from './alertData';

describe('mergeAlertsByEpisode', () => {
  it('merges an ended snapshot over the live episode with the same alert_id', () => {
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

  it('keeps a re-activated episode separate from a previously ended one', () => {
    const firstEpisode: Alert = {
      alert_id: 'flight_1:zone_a:warn:1700000000',
      flight_id: 'flight_1',
      zone: 'zone_a',
      rule: 'warn',
      active: false,
      activated_at: 1700000000,
      deactivated_at: 1700000100,
    };
    const reactivatedEpisode: Alert = {
      alert_id: 'flight_1:zone_a:warn:1700000300',
      flight_id: 'flight_1',
      zone: 'zone_a',
      rule: 'warn',
      active: true,
      activated_at: 1700000300,
    };

    const merged = mergeAlertsByEpisode([firstEpisode], [reactivatedEpisode]);
    expect(merged).toHaveLength(2);
    expect(merged.find((a) => a.alert_id === reactivatedEpisode.alert_id)?.active).toBe(true);
  });

  it('appends episodes that are not already in state', () => {
    const existing: Alert = {
      alert_id: 'a',
      flight_id: 'f',
      zone: 'z',
      rule: 'warn',
      active: true,
      activated_at: 1,
    };
    const incoming: Alert = {
      alert_id: 'b',
      flight_id: 'f',
      zone: 'z',
      rule: 'alert',
      active: true,
      activated_at: 2,
    };
    const merged = mergeAlertsByEpisode([existing], [incoming]);
    expect(merged.map((a) => a.alert_id)).toEqual(['a', 'b']);
  });
});

describe('endedByLabel', () => {
  const ended = (ended_by?: string | null): Alert => ({
    alert_id: 'a',
    active: false,
    activated_at: 100,
    deactivated_at: 200,
    ended_by,
  });

  it('labels each deactivation reason', () => {
    expect(endedByLabel(ended('cleared'))).toBe('left zone / conditions cleared');
    expect(endedByLabel(ended('lost'))).toBe('aircraft lost');
    expect(endedByLabel(ended('shutdown'))).toBe('engine shutdown');
    expect(endedByLabel(ended('restart'))).toBe('engine restart');
  });

  it('shows nothing when absent or while the episode is active', () => {
    expect(endedByLabel(ended(undefined))).toBeNull();
    expect(endedByLabel(ended(null))).toBeNull();
    expect(endedByLabel({ ...ended('lost'), active: true, deactivated_at: null })).toBeNull();
    expect(endedByLabel(ended('constructor'))).toBe('constructor');
  });

  it('adds the reason to the episode tooltip', () => {
    expect(alertTooltip(ended('lost'), 300)).toMatch(/Ended: .* \(aircraft lost\)/);
    expect(alertTooltip(ended(undefined), 300)).not.toMatch(/\(/);
  });

  it('keeps the reason when a later snapshot omits it', () => {
    const [merged] = mergeAlertsByEpisode([ended('cleared')], [ended(null)]);
    expect(endedByLabel(merged)).toBe('left zone / conditions cleared');
  });
});
