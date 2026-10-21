import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { FlightSummary, LiveMessage } from './api/types';

const NOW = Math.floor(Date.now() / 1000);

const flights: FlightSummary[] = [
  {
    flight_id: 'f1',
    icao: 'abc123',
    callsign: 'TEST1',
    model: 'Cessna 172',
    is_live: true,
    latitude: 35.7,
    longitude: -78.7,
    altitude: 1200,
    speed: 200,
    heading: 90,
    start_time: NOW - 600,
    timestamp: NOW,
    active_alerts: [],
  },
];

const socket = vi.hoisted(() => ({
  handlers: new Set<{
    onOpen?: () => void;
    onMessage?: (m: LiveMessage) => void;
    onClose?: (code?: number, reason?: string) => void;
  }>(),
}));

vi.mock('./api/liveSocket', () => ({
  connectLiveSocket: (h: Parameters<typeof socket.handlers.add>[0]) => {
    socket.handlers.add(h);
    queueMicrotask(() => h.onOpen?.());
    return () => socket.handlers.delete(h);
  },
  sendWsRequest: vi.fn(),
  isLiveSocketOpen: () => true,
}));

vi.mock('./api/rawSocket', () => ({
  connectRawSocket: () => () => {},
}));

vi.mock('./api/client', () => ({
  HISTORY_FETCH_CAP: 200,
  fetchFlights: vi.fn(async () => flights),
  fetchAlerts: vi.fn(async () => []),
  fetchStats: vi.fn(async () => ({
    live_flights: 1,
    active_alerts: 0,
    retained_flights: 0,
    historical_alerts: 0,
    engine_live: true,
  })),
  fetchZones: vi.fn(async () => ({ home: { latitude: 35, longitude: -78 }, zones: [] })),
  fetchConfig: vi.fn(async () => ({ home: { latitude: 35, longitude: -78 }, remember_planes: 60 })),
  fetchFlight: vi.fn(async () => ({ ...flights[0], registration: 'N123' })),
  fetchTelemetry: vi.fn(async () => [
    { timestamp: NOW - 10, latitude: 35.69, longitude: -78.69, altitude: 1100 },
    { timestamp: NOW, latitude: 35.7, longitude: -78.7, altitude: 1200 },
  ]),
}));

// Leaflet + MapLibre need a real canvas/WebGL; the map is exercised separately.
vi.mock('./components/MapView', () => ({
  MapView: ({ drawer }: { drawer: React.ReactNode }) => <div data-testid="map">{drawer}</div>,
}));

const { PortalApp } = await import('./PortalApp');

beforeEach(() => {
  socket.handlers.clear();
  window.history.replaceState(null, '', '/');
});

describe('PortalApp', () => {
  it('renders live flights, opens the drawer, and closes it with Escape', async () => {
    render(<PortalApp />);
    const row = await screen.findByRole('button', { name: /TEST1/ });
    fireEvent.click(row);

    const drawer = await screen.findByRole('complementary');
    await waitFor(() => expect(within(drawer).getByText('N123')).toBeInTheDocument());
    expect(window.location.search).toContain('flight=f1');

    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(window.location.search).not.toContain('flight=f1'));
  });

  it('applies pushed telemetry without re-fetching', async () => {
    render(<PortalApp />);
    await screen.findByRole('button', { name: /TEST1/ });
    act(() => {
      socket.handlers.forEach((h) =>
        h.onMessage?.({
          type: 'telemetry',
          timestamp: NOW + 1,
          telemetry: [{ flight_id: 'f2', icao: 'def456', timestamp: NOW + 1, latitude: 36, longitude: -79 }],
        }),
      );
    });
    expect(await screen.findByRole('button', { name: /DEF456/ })).toBeInTheDocument();
  });
});
