import { describe, expect, it, vi } from 'vitest';
import { HISTORY_FETCH_CAP } from '../api/client';
import { fetchHistoryPages } from './historyPages';

describe('fetchHistoryPages', () => {
  it('pages past the WS cap instead of shrinking the loaded window', async () => {
    const calls: { skip: number; limit: number }[] = [];
    const load = vi.fn(async (skip: number, limit: number) => {
      calls.push({ skip, limit });
      const start = skip;
      return Array.from({ length: limit }, (_, i) => start + i);
    });
    const result = await fetchHistoryPages(250, load);
    expect(result).not.toBeNull();
    expect(result!.items).toHaveLength(250);
    expect(result!.hasMore).toBe(true);
    expect(calls).toEqual([
      { skip: 0, limit: HISTORY_FETCH_CAP },
      { skip: HISTORY_FETCH_CAP, limit: 50 },
    ]);
  });

  it('stops and reports no more when a short page is returned', async () => {
    const load = vi.fn(async (skip: number, limit: number) => {
      if (skip === 0) return Array.from({ length: limit }, (_, i) => i);
      return [200, 201];
    });
    const result = await fetchHistoryPages(250, load);
    expect(result!.items).toHaveLength(HISTORY_FETCH_CAP + 2);
    expect(result!.hasMore).toBe(false);
  });

  it('propagates loader errors instead of treating them as an empty page', async () => {
    await expect(
      fetchHistoryPages(50, async () => {
        throw new Error('archive down');
      }),
    ).rejects.toThrow('archive down');
  });

  it('aborts when stillCurrent becomes false', async () => {
    let live = true;
    const load = vi.fn(async () => {
      live = false;
      return Array.from({ length: HISTORY_FETCH_CAP }, (_, i) => i);
    });
    const result = await fetchHistoryPages(250, load, () => live);
    expect(result).toBeNull();
  });
});
