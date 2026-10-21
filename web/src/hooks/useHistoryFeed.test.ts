import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FlightSummary } from '../api/types';
import { HISTORY_PAGE_LIMIT, mergeFirstPage, useHistoryFeed, type HistoryFilter } from './useHistoryFeed';

const api = vi.hoisted(() => ({
  fetchFlights: vi.fn(),
  fetchAlerts: vi.fn(),
  fetchStats: vi.fn(),
}));
vi.mock('../api/client', () => api);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const flight = (id: string): FlightSummary => ({ flight_id: id, icao: id });
const noFilter: HistoryFilter = { q: '', since: null, until: null };

beforeEach(() => {
  api.fetchFlights.mockReset();
  api.fetchAlerts.mockReset().mockResolvedValue([]);
  api.fetchStats.mockReset().mockResolvedValue(null);
});
afterEach(() => {
  vi.useRealTimers();
});

describe('mergeFirstPage', () => {
  const key = (x: { id: string }) => x.id;
  it('prepends new items, replaces known ones, and counts additions', () => {
    const existing = [{ id: 'b', v: 1 }, { id: 'c', v: 1 }];
    const { items, added } = mergeFirstPage(existing, [{ id: 'a', v: 2 }, { id: 'b', v: 2 }], key);
    expect(items).toEqual([{ id: 'a', v: 2 }, { id: 'b', v: 2 }, { id: 'c', v: 1 }]);
    expect(added).toBe(1);
  });
});

describe('useHistoryFeed', () => {
  it('a filter change during an in-flight fetch wins over the stale response', async () => {
    const stale = deferred<FlightSummary[]>();
    const fresh = deferred<FlightSummary[]>();
    api.fetchFlights.mockImplementation((_view: string, opts: { q?: string }) =>
      opts.q === 'new' ? fresh.promise : stale.promise,
    );

    const { result, rerender } = renderHook(({ filter }) => useHistoryFeed(true, filter), {
      initialProps: { filter: noFilter },
    });
    expect(api.fetchFlights).toHaveBeenCalledTimes(1);

    rerender({ filter: { q: 'new', since: null, until: null } });
    // The new filter must be fetched immediately, not skipped as "busy".
    expect(api.fetchFlights).toHaveBeenCalledTimes(2);
    expect(api.fetchFlights.mock.calls[1][1]).toMatchObject({ q: 'new', skip: 0 });

    await act(async () => {
      fresh.resolve([flight('match')]);
      await fresh.promise;
    });
    await act(async () => {
      stale.resolve([flight('stale')]);
      await stale.promise;
    });
    expect(result.current.flights.map((f) => f.flight_id)).toEqual(['match']);
    expect(result.current.isLoadingFlights).toBe(false);
  });

  it('retry clears the alerts spinner even when the retry fails', async () => {
    api.fetchFlights.mockResolvedValue([]);
    api.fetchAlerts.mockRejectedValue(new Error('boom'));
    const { result } = renderHook(() => useHistoryFeed(true, noFilter));
    await waitFor(() => expect(result.current.alertsError).toBe('Failed to load alerts.'));

    act(() => result.current.retry('alerts'));
    expect(result.current.isLoadingAlerts).toBe(true);
    await waitFor(() => expect(result.current.isLoadingAlerts).toBe(false));
    expect(result.current.alertsError).toBe('Failed to load alerts.');
  });

  it('periodic refresh re-reads only the first page and keeps loaded pages', async () => {
    vi.useFakeTimers();
    const page1 = Array.from({ length: HISTORY_PAGE_LIMIT }, (_, i) => flight(`f${i}`));
    const page2 = [flight('older')];
    api.fetchFlights.mockImplementation((_v: string, opts: { skip?: number }) =>
      Promise.resolve(opts.skip ? page2 : page1),
    );
    const { result } = renderHook(() => useHistoryFeed(true, noFilter));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await result.current.loadMore('flights');
    });
    expect(result.current.flights).toHaveLength(HISTORY_PAGE_LIMIT + 1);
    expect(api.fetchFlights.mock.lastCall?.[1]).toMatchObject({ skip: HISTORY_PAGE_LIMIT });

    api.fetchFlights.mockClear();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(api.fetchFlights).toHaveBeenCalledTimes(1);
    expect(api.fetchFlights.mock.calls[0][1]).toMatchObject({ skip: 0, limit: HISTORY_PAGE_LIMIT });
    expect(result.current.flights).toHaveLength(HISTORY_PAGE_LIMIT + 1);
  });
});
