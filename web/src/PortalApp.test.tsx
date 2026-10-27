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
  reject: false,
  handlers: new Set<{
    onOpen?: () => void;
    onMessage?: (m: LiveMessage) => void;
    onClose?: (code?: number, reason?: string) => void;
  }>(),
}));

vi.mock('./api/liveSocket', () => ({
  connectLiveSocket: (h: Parameters<typeof socket.handlers.add>[0]) => {
    socket.handlers.add(h);
    queueMicrotask(() =>
      socket.reject ? h.onClose?.(1008, 'origin not allowed') : h.onOpen?.(),
    );
    return () => socket.handlers.delete(h);
  },
  sendWsRequest: vi.fn(),
  isLiveSocketOpen: () => true,
}));

vi.mock('./api/rawSocket', () => ({
  connectRawSocket: () => () => {},
}));

vi.mock('./api/client', () => ({
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

// MapLibre needs a real canvas/WebGL; the map is exercised separately.
vi.mock('./components/MapView', () => ({
  MapView: ({ drawer }: { drawer: React.ReactNode }) => <div data-testid="map">{drawer}</div>,
}));

const { PortalApp } = await import('./PortalApp');

beforeEach(() => {
  socket.reject = false;
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

  it('shows one origin-rejected message everywhere, with nothing left loading', async () => {
    socket.reject = true;
    render(<PortalApp />);
    await waitFor(() =>
      expect(screen.getAllByText(/web\.allowed_hosts/).length).toBeGreaterThanOrEqual(2),
    );
    expect(screen.queryByText(/Loading/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
  });

  it('badges new live alerts until the alerts tab is opened', async () => {
    render(<PortalApp />);
    await screen.findByRole('button', { name: /TEST1/ });

    const alert = {
      alert_id: 'a1',
      flight_id: 'f1',
      icao: 'abc123',
      callsign: 'TEST1',
      zone: 'home',
      rule: 'low',
      active: true,
      activated_at: NOW,
    };
    act(() => {
      socket.handlers.forEach((h) => h.onMessage?.({ type: 'alerts', alerts: [alert] }));
    });
    expect(screen.queryByRole('tab', { name: /1 new/ })).toBeNull();

    act(() => {
      socket.handlers.forEach((h) =>
        h.onMessage?.({
          type: 'alerts',
          alerts: [alert, { ...alert, alert_id: 'a2' }],
        }),
      );
    });
    const badge = await screen.findByRole('tab', { name: /Alerts.*1 new/ });
    fireEvent.click(badge);
    await waitFor(() => expect(screen.queryByRole('tab', { name: /1 new/ })).toBeNull());
  });
});
