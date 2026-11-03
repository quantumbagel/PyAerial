import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import type { LiveSocketHandlers } from '../api/liveSocket';
import type { FlightDetail, TelemetryPoint } from '../api/types';
import type { MapViewHandle } from '../components/MapView';
import { useFlightSelection } from './useFlightSelection';
import { makeFlightDetail, makePoint } from '../test/fixtures';

const api = vi.hoisted(() => ({
  fetchFlight: vi.fn(),
  fetchTelemetryPage: vi.fn(),
  fetchAlerts: vi.fn(),
}));
vi.mock('../api/client', () => api);

const socket = vi.hoisted(() => ({
  open: false,
  handlers: new Set<LiveSocketHandlers>(),
}));
vi.mock('../api/liveSocket', () => ({
  connectLiveSocket: (handlers: LiveSocketHandlers) => {
    socket.handlers.add(handlers);
    return () => socket.handlers.delete(handlers);
  },
  isLiveSocketOpen: () => socket.open,
}));

const detail: FlightDetail = makeFlightDetail({ flight_id: 'f1', icao: 'abc123', is_live: true });
const points: TelemetryPoint[] = [makePoint({ timestamp: 1, latitude: 35, longitude: -78 })];
const mapRef = { current: { panToAlert: vi.fn(), focusOn: vi.fn() } as MapViewHandle };

function reconnect() {
  socket.open = true;
  socket.handlers.forEach((h) => h.onOpen?.());
}

beforeEach(() => {
  socket.open = false;
  socket.handlers.clear();
  api.fetchFlight.mockReset().mockResolvedValue(detail);
  api.fetchTelemetryPage.mockReset().mockResolvedValue({ points, truncated: false });
  api.fetchAlerts.mockReset().mockResolvedValue([]);
});

describe('useFlightSelection', () => {
  it('retries a selection cut off by a disconnect once the socket is back', async () => {
    api.fetchFlight.mockRejectedValueOnce(new ApiError('disconnected', 'Connection closed'));
    const { result } = renderHook(() => useFlightSelection({ portalView: 'live', mapRef }));

    await act(async () => {
      await result.current.selectFlight('f1');
    });
    expect(result.current.selectionError).toBeNull();
    expect(result.current.isLoading).toBe(true);

    await act(async () => {
      reconnect();
    });
    expect(api.fetchFlight).toHaveBeenCalledTimes(2);
    expect(result.current.flightDetail).toEqual(detail);
    expect(result.current.isLoading).toBe(false);
  });

  it('does not retry after the drawer was closed', async () => {
    api.fetchFlight.mockRejectedValueOnce(new ApiError('disconnected', 'Connection closed'));
    const { result } = renderHook(() => useFlightSelection({ portalView: 'live', mapRef }));
    await act(async () => {
      await result.current.selectFlight('f1');
    });
    act(() => result.current.closeDrawer());
    await act(async () => {
      reconnect();
    });
    expect(api.fetchFlight).toHaveBeenCalledTimes(1);
  });

  it('shows the error for a timeout while the socket is still up', async () => {
    socket.open = true;
    api.fetchFlight.mockRejectedValueOnce(new ApiError('timeout', 'Request timed out'));
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { result } = renderHook(() => useFlightSelection({ portalView: 'live', mapRef }));
    await act(async () => {
      await result.current.selectFlight('f1');
    });
    expect(result.current.selectionError).toMatch(/too long/);
    expect(result.current.isLoading).toBe(false);
    error.mockRestore();
  });

  it('reports when the server truncated the telemetry', async () => {
    api.fetchTelemetryPage.mockResolvedValue({ points, truncated: true });
    const { result } = renderHook(() => useFlightSelection({ portalView: 'history', mapRef }));
    await act(async () => {
      await result.current.selectFlight('f1');
    });
    expect(result.current.telemetryTruncated).toBe(true);
    act(() => result.current.closeDrawer());
    expect(result.current.telemetryTruncated).toBe(false);
  });
});
