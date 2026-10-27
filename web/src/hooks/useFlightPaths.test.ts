import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { FlightSummary, TelemetryPoint } from '../api/types';
import { MAX_PATH_FETCHES, pathNeedsFetch, useFlightPaths } from './useFlightPaths';

const api = vi.hoisted(() => ({
  fetchTelemetry: vi.fn(),
  fetchAlerts: vi.fn(),
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

const flight = (id: string, lat = 35, lon = -78): FlightSummary => ({
  flight_id: id,
  icao: id,
  latitude: lat,
  longitude: lon,
});

const point = (id: string, ts: number, lat: number, lon: number): TelemetryPoint => ({
  flight_id: id,
  timestamp: ts,
  latitude: lat,
  longitude: lon,
});

beforeEach(() => {
  api.fetchTelemetry.mockReset().mockResolvedValue([]);
  api.fetchAlerts.mockReset().mockResolvedValue([]);
});

describe('pathNeedsFetch', () => {
  const empty = new Set<string>();

  it('queues ids that have never been fetched', () => {
    expect(pathNeedsFetch('a', {}, empty, empty)).toBe(true);
  });

  it('treats a successful empty path as terminal', () => {
    expect(pathNeedsFetch('a', { a: [] }, empty, empty)).toBe(false);
  });

  it('refetches when coords were pruned after a successful fetch', () => {
    expect(pathNeedsFetch('a', {}, empty, empty)).toBe(true);
  });

  it('does not re-queue pending or failed ids', () => {
    expect(pathNeedsFetch('a', {}, new Set(['a']), empty)).toBe(false);
    expect(pathNeedsFetch('a', {}, empty, new Set(['a']))).toBe(false);
  });

  it('still fetches other ids when one empty success is parked', () => {
    expect(pathNeedsFetch('b', { a: [] }, empty, empty)).toBe(true);
  });
});

describe('useFlightPaths', () => {
  it('fetches the selected flight and ignores a late result after deselection', async () => {
    const first = deferred<TelemetryPoint[]>();
    api.fetchTelemetry.mockReturnValueOnce(first.promise);
    const { result, rerender } = renderHook(
      ({ id }) => useFlightPaths('live', id, [flight('a')]),
      { initialProps: { id: 'a' as string | null } },
    );

    await waitFor(() => expect(api.fetchTelemetry).toHaveBeenCalledWith('a', 'live'));
    rerender({ id: null });
    await act(async () => {
      first.resolve([point('a', 1, 35, -78)]);
      await first.promise;
    });
    expect(result.current.pathCoords).toEqual({});
  });

  it('caps concurrent overlay fetches and starts the next when one finishes', async () => {
    const blocked = Array.from({ length: MAX_PATH_FETCHES + 1 }, () =>
      deferred<TelemetryPoint[]>(),
    );
    api.fetchTelemetry.mockImplementation((id: string) => {
      const index = Number(id.slice(1));
      return blocked[index].promise;
    });
    const flights = blocked.map((_, i) => flight(`f${i}`));
    const { result } = renderHook(() => useFlightPaths('live', null, flights));

    act(() => result.current.setShowAllPaths(true));
    await waitFor(() => expect(api.fetchTelemetry).toHaveBeenCalledTimes(MAX_PATH_FETCHES));

    await act(async () => {
      blocked[0].resolve([point('f0', 1, 35, -78)]);
      await blocked[0].promise;
    });
    await waitFor(() => expect(api.fetchTelemetry).toHaveBeenCalledTimes(MAX_PATH_FETCHES + 1));
    expect(result.current.pathCoords.f0).toEqual([[35, -78]]);
  });

  it('does not retry a failed overlay fetch', async () => {
    api.fetchTelemetry.mockRejectedValueOnce(new Error('boom'));
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { result } = renderHook(() => useFlightPaths('live', null, [flight('a'), flight('b')]));
    act(() => result.current.setShowAllPaths(true));
    await waitFor(() => expect(api.fetchTelemetry).toHaveBeenCalled());
    const calls = api.fetchTelemetry.mock.calls.length;
    await act(async () => {
      await Promise.resolve();
    });
    expect(api.fetchTelemetry).toHaveBeenCalledTimes(calls);
    expect(result.current.pathCoords.a).toBeUndefined();
    warn.mockRestore();
  });

  it('keeps an empty successful path terminal', async () => {
    api.fetchTelemetry.mockResolvedValue([]);
    const { result } = renderHook(() => useFlightPaths('live', 'a', [flight('a')]));
    await waitFor(() => expect(result.current.pathCoords).toHaveProperty('a'));
    expect(result.current.pathCoords.a).toEqual([]);
    expect(api.fetchTelemetry).toHaveBeenCalledTimes(1);
  });

  it('appends live positions only onto paths that already exist', async () => {
    api.fetchTelemetry.mockResolvedValue([point('a', 1, 35, -78)]);
    const { result } = renderHook(() => useFlightPaths('live', 'a', [flight('a')]));
    await waitFor(() => expect(result.current.pathCoords.a).toEqual([[35, -78]]));

    act(() => {
      result.current.appendFlightPositions([
        flight('a', 35.1, -78.1),
        flight('b', 36, -79),
      ]);
    });
    expect(result.current.pathCoords.a).toEqual([
      [35, -78],
      [35.1, -78.1],
    ]);
    expect(result.current.pathCoords.b).toBeUndefined();
  });

  it('prunes overlay paths when a flight leaves the list', async () => {
    api.fetchTelemetry.mockResolvedValue([point('a', 1, 35, -78)]);
    const { result, rerender } = renderHook(
      ({ flights }) => useFlightPaths('live', null, flights),
      { initialProps: { flights: [flight('a')] } },
    );
    act(() => result.current.setShowAllPaths(true));
    await waitFor(() => expect(result.current.pathCoords.a).toEqual([[35, -78]]));

    rerender({ flights: [flight('b')] });
    await waitFor(() => expect(result.current.pathCoords.a).toBeUndefined());
  });
});
