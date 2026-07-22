import { describe, expect, it } from 'vitest';
import { alertColorFor, zoneColorFor } from './zoneColors';

describe('zoneColors', () => {
  const zones = [
    {
      name: 'aerpaw',
      color: '#f59e0b',
      rules: [{ name: 'warn' }, { name: 'alert', color: '#ff0000' }],
    },
    {
      name: 'cool',
      color: '#22d3ee',
      rules: [{ name: 'cool' }],
    },
  ];

  it('uses global alert_colors for rule-based badge colors', () => {
    const colors = alertColorFor('aerpaw', 'warn', zones, { warn: '#123456' });
    expect(colors.fill).toBe('#123456');
  });

  it('prefers per-zone rule color over global alert_colors', () => {
    const colors = alertColorFor('aerpaw', 'alert', zones, { alert: '#ef4444' });
    expect(colors.fill).toBe('#ff0000');
  });

  it('falls back to zone color when rule color is not configured', () => {
    const colors = alertColorFor('cool', 'cool', zones, { cool: '#22d3ee' });
    expect(colors.fill).toBe('#22d3ee');
  });

  it('uses configured zone color for map polygons', () => {
    const colors = zoneColorFor('cool', zones);
    expect(colors.fill).toBe('#22d3ee');
  });
});
