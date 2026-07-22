import { renderHook, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { usePortalData } from './usePortalData';
import { resetLiveSocketForTests } from '../api/liveSocket';

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;
  static CLOSED = 3;
  readyState = MockWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string) {
    MockWebSocket.instances.push(this);
    queueMicrotask(() => this.onopen?.());
  }

  send() {}
  close() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }

  receive(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
}

vi.mock('../api/client', () => ({
  fetchZones: vi.fn().mockResolvedValue({ home: null, zones: [] }),
  fetchConfig: vi.fn().mockResolvedValue({}),
  fetchFlights: vi.fn().mockResolvedValue([]),
  fetchAlerts: vi.fn().mockResolvedValue([]),
}));

describe('usePortalData path updates', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    resetLiveSocketForTests();
    vi.stubGlobal('WebSocket', MockWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    resetLiveSocketForTests();
    vi.unstubAllGlobals();
  });

  it('updates pathCoords for non-selected flights when showAllPaths is true', async () => {
    const setPathCoords = vi.fn();
    const showAllPathsRef = { current: true };
    const activeFlightIdRef = { current: null };

    renderHook(() =>
      usePortalData({
        portalView: 'live',
        setPortalView: () => {},
        activeFlightIdRef,
        showAllPathsRef,
        setPathCoords,
        appendSelectedTelemetry: () => {},
        loadFlightAlerts: async () => [],
        onNewAlerts: () => {},
        resetSelection: () => {},
        resetPaths: () => {},
        stopDetailPoll: () => {},
      }),
    );

    const ws = MockWebSocket.instances[0];

    // Simulate incoming telemetry for a non-selected flight (N12345)
    await act(async () => {
      ws.receive({
        type: 'telemetry',
        telemetry: [
          { flight_id: 'N12345', latitude: 35.1, longitude: -78.2, timestamp: 1000 },
        ],
      });
    });

    expect(setPathCoords).toHaveBeenCalled();
    const updateFn = setPathCoords.mock.calls[setPathCoords.mock.calls.length - 1][0];
    const initialCoords = { N12345: [[35.0, -78.1]] as [number, number][] };
    const updated = updateFn(initialCoords);

    expect(updated.N12345).toEqual([
      [35.0, -78.1],
      [35.1, -78.2],
    ]);
  });
});
