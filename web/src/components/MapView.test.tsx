import { render } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import type { FlightSummary } from '../api/types';
import { MapView, type MapViewHandle } from './MapView';

vi.mock('./mapLibreLayer', () => ({ addBasemap: vi.fn() }));

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

const flight = (overrides: Partial<FlightSummary> = {}): FlightSummary => ({
  flight_id: 'f1',
  icao: 'abc123',
  callsign: '<img src=x onerror=alert(1)>',
  is_live: true,
  latitude: 35,
  longitude: -78,
  heading: 10,
  active_alerts: [],
  ...overrides,
});

const mapRef = { current: { map: null, panToAlert: () => {}, focusOn: () => {} } as MapViewHandle };

function props(flights: FlightSummary[], overrides: Partial<ComponentProps<typeof MapView>> = {}) {
  return {
    flights,
    filteredFlights: flights,
    activeFlightId: null,
    selectedTelemetryPoint: null,
    followSelectedPlane: false,
    zonesVisible: true,
    showAllPaths: false,
    zonesData: null,
    appConfig: null,
    pathCoords: {},
    pathTelemetry: {},
    pathAlerts: {},
    drawerOpen: false,
    onSelectFlight: vi.fn(),
    onFollowDisabled: vi.fn(),
    onToggleFollow: vi.fn(),
    onToggleZones: vi.fn(),
    onTogglePaths: vi.fn(),
    mapRef,
    ...overrides,
  };
}

describe('MapView markers', () => {
  it('rotates the existing icon on heading changes instead of replacing it', () => {
    const { container, rerender } = render(<MapView {...props([flight()])} />);
    const icon = container.querySelector('.plane-icon');
    const svg = container.querySelector<SVGElement>('.plane-icon__svg');
    expect(svg?.style.transform).toBe('rotate(10deg)');

    rerender(<MapView {...props([flight({ heading: 95, latitude: 35.01 })])} />);
    expect(container.querySelector('.plane-icon')).toBe(icon);
    expect(container.querySelector<SVGElement>('.plane-icon__svg')?.style.transform).toBe('rotate(95deg)');
  });

  it('labels markers with text, never parsing names as HTML', () => {
    const { container } = render(<MapView {...props([flight()])} />);
    const icon = container.querySelector('.plane-icon');
    expect(icon?.getAttribute('aria-label')).toContain('<img src=x onerror=alert(1)>');
    expect(container.querySelector('img[src="x"]')).toBeNull();
  });

  it('builds zone labels as text nodes', () => {
    const { container } = render(
      <MapView
        {...props([], {
          zonesData: {
            home: { latitude: 35, longitude: -78 },
            zones: [
              {
                name: '<b>evil</b>',
                coordinates: [
                  [35, -78],
                  [35.1, -78],
                  [35.1, -78.1],
                ],
                rules: [],
              },
            ],
          },
        })}
      />,
    );
    expect(container.querySelector('b')).toBeNull();
  });
});
