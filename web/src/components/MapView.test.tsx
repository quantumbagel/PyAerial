import { render, waitFor } from '@testing-library/react';
import type { ComponentProps, RefObject } from 'react';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FlightSummary } from '../api/types';
import { MapView, type MapViewHandle } from './MapView';
import { SRC } from './mapLibreEngine';

type SourceStub = {
  data: { type: string; features: Array<{ properties: Record<string, unknown> }> };
  setData: (data: SourceStub['data']) => void;
};

type EaseToOpts = { center?: [number, number]; zoom?: number; offset?: [number, number] };

const { maps, MapMock, PopupMock, setWorkerUrl } = vi.hoisted(() => {
  const maps: MapMock[] = [];

  class MapMock {
    container: HTMLElement;
    zoom: number;
    sources: Record<string, SourceStub> = {};
    images = new Set<string>();
    lastEaseTo: EaseToOpts | null = null;
    lastJumpTo: EaseToOpts | null = null;
    removed = false;
    touchZoomRotate = { disableRotation: vi.fn() };
    private listeners = new Map<string, Array<(...args: unknown[]) => void>>();

    constructor(options: { container: HTMLElement; zoom?: number }) {
      this.container = options.container;
      this.zoom = options.zoom ?? 8;
      maps.push(this);
      queueMicrotask(() => this.emit('load'));
    }

    on(type: string, layerOrFn: unknown, maybeFn?: unknown) {
      const handler = typeof layerOrFn === 'function' ? layerOrFn : maybeFn;
      if (typeof handler === 'function') {
        const list = this.listeners.get(type) ?? [];
        list.push(handler as (...args: unknown[]) => void);
        this.listeners.set(type, list);
      }
      return this;
    }

    emit(type: string) {
      for (const handler of this.listeners.get(type) ?? []) handler();
    }

    addSource(id: string, spec: { data?: SourceStub['data'] }) {
      const source: SourceStub = {
        data: spec.data ?? { type: 'FeatureCollection', features: [] },
        setData: (data) => {
          source.data = data;
        },
      };
      this.sources[id] = source;
    }

    addLayer() {}

    getSource(id: string) {
      return this.sources[id];
    }

    hasImage(id: string) {
      return this.images.has(id);
    }

    addImage(id: string) {
      this.images.add(id);
    }

    getZoom() {
      return this.zoom;
    }

    easeTo(opts: EaseToOpts) {
      this.lastEaseTo = opts;
      if (opts.zoom != null) this.zoom = opts.zoom;
    }

    jumpTo(opts: EaseToOpts) {
      this.lastJumpTo = opts;
      if (opts.zoom != null) this.zoom = opts.zoom;
    }

    panBy() {}
    zoomIn() {
      this.zoom += 1;
    }
    zoomOut() {
      this.zoom -= 1;
    }
    resize() {}
    remove() {
      this.removed = true;
    }
    project() {
      return { x: 0, y: 0 };
    }
    getContainer() {
      return this.container;
    }
    getCanvas() {
      return { style: {} as CSSStyleDeclaration };
    }
    queryRenderedFeatures() {
      return [];
    }
  }

  class PopupMock {
    setLngLat() {
      return this;
    }
    setDOMContent() {
      return this;
    }
    addTo() {
      return this;
    }
    remove() {
      return this;
    }
  }

  return { maps, MapMock, PopupMock, setWorkerUrl: vi.fn() };
});

vi.mock('maplibre-gl', () => ({
  Map: MapMock,
  Popup: PopupMock,
  setWorkerUrl,
}));

vi.mock('maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url', () => ({
  default: '/mock-maplibre-worker.js',
}));

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

beforeEach(() => {
  maps.length = 0;
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

function mapRefHandle(): RefObject<MapViewHandle> {
  return { current: { panToAlert: () => {}, focusOn: () => {} } };
}

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
    mapRef: mapRefHandle(),
    ...overrides,
  };
}

function planesSource() {
  return maps[0]?.getSource(SRC.planes);
}

function planeProperties() {
  return planesSource()?.data.features[0]?.properties;
}

async function readyMap() {
  await waitFor(() => {
    expect(maps[0]?.getSource(SRC.planes)).toBeTruthy();
  });
}

describe('MapView markers', () => {
  it('rotates the existing icon on heading changes instead of replacing it', async () => {
    const mapRef = mapRefHandle();
    const { rerender } = render(<MapView {...props([flight()], { mapRef })} />);
    await readyMap();
    const first = planeProperties();
    expect(first?.heading).toBe(10);
    const icon = first?.icon;
    expect(icon).toEqual(expect.any(String));
    const images = maps[0].images.size;

    rerender(<MapView {...props([flight({ heading: 95, latitude: 35.01 })], { mapRef })} />);
    const next = planeProperties();
    expect(next?.icon).toBe(icon);
    expect(next?.heading).toBe(95);
    expect(maps[0].images.size).toBe(images);
  });

  it('labels markers with text, never parsing names as HTML', async () => {
    const { container } = render(<MapView {...props([flight()])} />);
    await readyMap();
    expect(planeProperties()?.label).toContain('<img src=x onerror=alert(1)>');
    expect(container.querySelector('img[src="x"]')).toBeNull();
  });

  it('builds zone labels as text properties, not HTML', async () => {
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
    await readyMap();
    const zones = maps[0].getSource(SRC.zones)?.data.features ?? [];
    expect(zones[0]?.properties.name).toBe('<b>evil</b>');
    expect(container.querySelector('b')).toBeNull();
  });

  it('marks the selected flight in source data', async () => {
    const mapRef = mapRefHandle();
    const { rerender } = render(<MapView {...props([flight()], { mapRef })} />);
    await readyMap();
    expect(planeProperties()?.selected).toBe(false);

    rerender(<MapView {...props([flight()], { mapRef, activeFlightId: 'f1' })} />);
    expect(planeProperties()?.selected).toBe(true);
    expect(planeProperties()?.z).toBe(1);
  });
});

describe('MapView handle', () => {
  it('focusOn eases to the point at least at minZoom', async () => {
    const mapRef = mapRefHandle();
    render(<MapView {...props([flight()], { mapRef })} />);
    await readyMap();
    mapRef.current.focusOn(35.5, -78.5, 11);
    expect(maps[0].lastEaseTo).toMatchObject({ center: [-78.5, 35.5], zoom: 11 });
  });

  it('panToAlert eases to the point at least at zoom 14', async () => {
    const mapRef = mapRefHandle();
    render(<MapView {...props([flight()], { mapRef })} />);
    await readyMap();
    mapRef.current.panToAlert(35.2, -78.2);
    expect(maps[0].lastEaseTo).toMatchObject({ center: [-78.2, 35.2], zoom: 14 });
  });
});
