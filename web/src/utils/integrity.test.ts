import { describe, expect, it } from 'vitest';
import type { Integrity } from '../api/types';
import { checkLabel, integrityLabel, integrityScore, isFlagged, sortedChecks } from './integrity';

const doc: Integrity = {
  score: 0.3,
  status: 'spoofed',
  checks: {
    timing: { status: 'pass', detail: 'ok' },
    tdoa: { status: 'fail', detail: 'misses by 40 km' },
    rssi_range: { status: 'warn', detail: '+12 dB' },
    aoa: { status: 'fail', detail: 'off by 60°' },
  },
};

describe('integrity utils', () => {
  it('orders failures first', () => {
    expect(sortedChecks(doc).map(([name]) => name)).toEqual(['aoa', 'tdoa', 'rssi_range', 'timing']);
  });

  it('flags only suspect and spoofed', () => {
    expect(isFlagged(doc)).toBe(true);
    expect(isFlagged({ ...doc, status: 'suspect' })).toBe(true);
    expect(isFlagged({ ...doc, status: 'ok' })).toBe(false);
    expect(isFlagged(undefined)).toBe(false);
  });

  it('labels statuses and checks', () => {
    expect(integrityLabel(doc)).toBe('Likely spoofed');
    expect(integrityLabel(null)).toBe('Not enough evidence');
    expect(checkLabel('aoa')).toBe('Angle of arrival');
    expect(checkLabel('new_check')).toBe('new check');
  });
});

describe('integrityScore', () => {
  it('is null when no check had evidence (status unknown) or the score is missing', () => {
    expect(integrityScore({ score: 1, status: 'unknown', checks: {} })).toBeNull();
    expect(integrityScore(null)).toBeNull();
    expect(integrityScore({ ...doc, score: Number.NaN })).toBeNull();
    expect(integrityScore(doc)).toBe(0.3);
  });

  it('orders checks with an unexpected status after the known ones', () => {
    const odd: Integrity = {
      score: 0.5,
      status: 'suspect',
      checks: {
        zeta: { status: 'other' as 'pass', detail: '' },
        aoa: { status: 'fail', detail: '' },
        timing: { status: 'pass', detail: '' },
      },
    };
    expect(sortedChecks(odd).map(([name]) => name)).toEqual(['aoa', 'timing', 'zeta']);
  });
});
