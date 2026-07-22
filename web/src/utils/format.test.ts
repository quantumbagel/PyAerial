import { describe, expect, it } from 'vitest';
import { formatActiveAlerts, formatFlightAlertSummary, formatZoneRule } from './format';

describe('formatZoneRule', () => {
  it('formats zone and rule with middle dot separator', () => {
    expect(formatZoneRule('aerpaw', 'cool')).toBe('aerpaw · cool');
  });

  it('adds a live suffix when requested', () => {
    expect(formatZoneRule('aerpaw', 'cool', { live: true })).toBe('aerpaw · cool (Live)');
  });

  it('falls back to zone and rule placeholders', () => {
    expect(formatZoneRule()).toBe('zone · rule');
  });
});

describe('alert summaries', () => {
  it('uses zone · rule formatting in active alert summaries', () => {
    const summary = formatFlightAlertSummary({
      is_live: true,
      active_alerts: [{ zone: 'aerpaw', rule: 'warn', activated_at: 1_700_000_000 }],
    });
    expect(summary).toContain('aerpaw · warn');
  });

  it('uses zone · rule formatting in formatActiveAlerts', () => {
    expect(
      formatActiveAlerts([{ zone: 'cool', rule: 'cool', activated_at: 1_700_000_000 }]),
    ).toContain('cool · cool');
  });
});
