import { describe, expect, it } from 'vitest';
import { localDayEndSeconds, localDayStartSeconds } from './historyFilters';

describe('history date filters', () => {
  it('returns null for an empty date', () => {
    expect(localDayStartSeconds('')).toBeNull();
    expect(localDayEndSeconds('')).toBeNull();
  });

  it('covers one local calendar day', () => {
    const start = localDayStartSeconds('2024-06-15');
    const end = localDayEndSeconds('2024-06-15');
    expect(start).not.toBeNull();
    expect(end).not.toBeNull();
    expect(end! - start!).toBeGreaterThan(86000);
    expect(end! - start!).toBeLessThan(87000);
  });
});
