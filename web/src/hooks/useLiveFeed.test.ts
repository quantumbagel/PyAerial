import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { LiveSocketHandlers } from '../api/liveSocket';
import type { Alert, FlightSummary, ServerStats } from '../api/types';
import { HIDDEN_PAUSE_MS, HIDDEN_STREAMS, IDLE_STREAMS, useLiveFeed } from './useLiveFeed';

const api = vi.hoisted(() => ({
  fetchFlights: vi.fn(),
  fetchAlerts: vi.fn(),
  fetchStats: vi.fn(),
}));
vi.mock('../api/client', () => api);

const socket = vi.hoisted(() => ({
  handlers: null as LiveSocketHandlers | null,
  setLiveStreams: vi.fn(),
}));
vi.mock('../api/liveSocket', () => ({
  setLiveStreams: socket.setLiveStreams,
  connectLiveSocket: (handlers: LiveSocketHandlers) => {
    socket.handlers = handlers;
    return () => {
      if (socket.handlers === handlers) socket.handlers = null;
    };
  },
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const flight = (id: string, overrides: Partial<FlightSummary> = {}): FlightSummary => ({
  flight_id: id,
  icao: id,
  is_live: true,
  latitude: 35,
  longitude: -78,
  timestamp: 100,
  ...overrides,
});

const stats = (overrides: Partial<ServerStats> = {}): ServerStats => ({
  live_flights: 1,
  active_alerts: 0,
  retained_flights: 0,
  historical_alerts: 0,
  redis: true,
  history: true,
  engine_seen_at: 100,
  engine_live: true,
  ...overrides,
});

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}

afterEach(() => {
  vi.useRealTimers();
  setVisibility('visible');
});

beforeEach(() => {
  socket.handlers = null;
  socket.setLiveStreams.mockReset();
  api.fetchFlights.mockReset();
  api.fetchAlerts.mockReset().mockResolvedValue([]);
  api.fetchStats.mockReset().mockResolvedValue(stats());
});

describe('useLiveFeed', () => {
  it('a push that arrives while the reconnect fetch is in flight wins over its answer', async () => {
    const flightsReq = deferred<FlightSummary[]>();
    const statsReq = deferred<ServerStats>();
    api.fetchFlights.mockReturnValue(flightsReq.promise);
    api.fetchStats.mockReturnValue(statsReq.promise);
    const { result } = renderHook(() => useLiveFeed(true));

    act(() => socket.handlers!.onOpen!());
    // The engine stopped after the RPCs were answered but before they arrived:
    // pushes only follow changes, so nothing would ever correct a stale answer.
    act(() => {
      socket.handlers!.onMessage!({ type: 'flights', flights: [] });
      socket.handlers!.onMessage!({ type: 'stats', stats: stats({ engine_live: false, live_flights: 0 }) });
    });
    await act(async () => {
      flightsReq.resolve([flight('gone')]);
      statsReq.resolve(stats({ engine_live: true }));
      await flightsReq.promise;
      await statsReq.promise;
    });

    expect(result.current.flights).toEqual([]);
    expect(result.current.stats?.engine_live).toBe(false);
    expect(result.current.isLoadingFlights).toBe(false);
  });

  it('uses the fetch answer when no push arrived (manual retry on an open socket)', async () => {
    api.fetchFlights.mockResolvedValue([flight('a')]);
    const { result } = renderHook(() => useLiveFeed(true));
    await act(async () => {
      socket.handlers!.onOpen!();
    });
    expect(result.current.flights.map((f) => f.flight_id)).toEqual(['a']);
    expect(result.current.stats?.engine_live).toBe(true);
  });

  it('prunes fetched alerts against the pushed flight list when that is newer', async () => {
    const flightsReq = deferred<FlightSummary[]>();
    const alertsReq = deferred<Alert[]>();
    api.fetchFlights.mockReturnValue(flightsReq.promise);
    api.fetchAlerts.mockReturnValue(alertsReq.promise);
    const { result } = renderHook(() => useLiveFeed(true));

    act(() => socket.handlers!.onOpen!());
    act(() => socket.handlers!.onMessage!({ type: 'flights', flights: [flight('kept')] }));
    await act(async () => {
      flightsReq.resolve([flight('kept'), flight('gone')]);
      alertsReq.resolve([
        { alert_id: 'k', flight_id: 'kept', active: true, activated_at: 90 },
        { alert_id: 'g', flight_id: 'gone', active: true, activated_at: 90 },
      ]);
      await flightsReq.promise;
      await alertsReq.promise;
    });

    expect(result.current.flights.map((f) => f.flight_id)).toEqual(['kept']);
    expect(result.current.alerts.map((a) => a.alert_id)).toEqual(['k']);
  });

  it('pauses flights/telemetry in a tab hidden for a while and resumes them when shown', () => {
    vi.useFakeTimers();
    api.fetchFlights.mockResolvedValue([]);
    const onResume = vi.fn();
    const { unmount } = renderHook(() => useLiveFeed(true, { onResume }));

    act(() => setVisibility('hidden'));
    act(() => vi.advanceTimersByTime(HIDDEN_PAUSE_MS - 1));
    expect(socket.setLiveStreams).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(socket.setLiveStreams).toHaveBeenLastCalledWith(HIDDEN_STREAMS);

    act(() => setVisibility('visible'));
    expect(socket.setLiveStreams).toHaveBeenLastCalledWith(null);
    expect(onResume).toHaveBeenCalledTimes(1);

    // A quick switch away and back changes nothing.
    act(() => setVisibility('hidden'));
    act(() => vi.advanceTimersByTime(1000));
    act(() => setVisibility('visible'));
    act(() => vi.advanceTimersByTime(HIDDEN_PAUSE_MS));
    expect(socket.setLiveStreams).toHaveBeenCalledTimes(2);
    expect(onResume).toHaveBeenCalledTimes(1);

    // Leaving Live while paused restores every stream.
    act(() => setVisibility('hidden'));
    act(() => vi.advanceTimersByTime(HIDDEN_PAUSE_MS));
    unmount();
    expect(socket.setLiveStreams).toHaveBeenLastCalledWith(null);
  });

  it('keeps only stats while Live is not shown and restores every stream on return', () => {
    api.fetchFlights.mockResolvedValue([]);
    const { rerender } = renderHook(({ enabled }) => useLiveFeed(enabled), {
      initialProps: { enabled: false },
    });
    expect(socket.setLiveStreams).toHaveBeenLastCalledWith(IDLE_STREAMS);
    rerender({ enabled: true });
    expect(socket.setLiveStreams).toHaveBeenLastCalledWith(null);
    rerender({ enabled: false });
    expect(socket.setLiveStreams).toHaveBeenLastCalledWith(IDLE_STREAMS);
  });
});
