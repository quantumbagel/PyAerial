import { useEffect, useRef, useState } from 'react';
import type { Map as MapLibreMap } from 'maplibre-gl';
import type { Alert, AppConfig, FlightSummary, TelemetryPoint, ZonesData } from '../api/types';
import { alertVisual, primaryAlert } from '../utils/alertData';
import { buildAlertPathSegments } from '../utils/alertPathSegments';
import {
  LIST_SEP,
  displayCallsign,
  formatAltitudeCompact,
  formatDateTime,
  formatSpeedCompact,
  formatZoneRule,
} from '../utils/format';
import { integrityLabel, isFlagged } from '../utils/integrity';
import {
  pathStyleForFlight,
  planeHeading,
  planeIconId,
  planeStyle,
  renderPlaneIcon,
} from '../utils/planeIcon';
import { zoneColorFor } from '../utils/zoneColors';
import { MapToolbar } from './MapToolbar';

export interface MapViewHandle {
  panToAlert: (lat: number, lon: number) => void;
  /** Center on a point (clear of the details drawer), zooming in to at least `minZoom`. */
  focusOn: (lat: number, lon: number, minZoom: number) => void;
}

interface MapViewProps {
  flights: FlightSummary[];
  filteredFlights: FlightSummary[];
  activeFlightId: string | null;
  selectedTelemetryPoint: TelemetryPoint | null;
  followSelectedPlane: boolean;
  zonesVisible: boolean;
  showAllPaths: boolean;
  zonesData: ZonesData | null;
  appConfig: AppConfig | null;
  pathCoords: Record<string, [number, number][]>;
  pathTelemetry: Record<string, TelemetryPoint[]>;
  pathAlerts: Record<string, Alert[]>;
  drawerOpen: boolean;
  onSelectFlight: (flightId: string) => void;
  onFollowDisabled: () => void;
  onToggleFollow: () => void;
  onToggleZones: () => void;
  onTogglePaths: () => void;
  onToggleSidebar?: () => void;
  sidebarOpen?: boolean;
  sidebarId?: string;
  mapRef: React.RefObject<MapViewHandle>;
  drawer?: React.ReactNode;
  /** Status line placed under the toolbar so it never covers the controls. */
  banner?: React.ReactNode;
}

/** Below this width the drawer is a bottom sheet (keep in sync with components.css). */
export const BOTTOM_SHEET_QUERY = '(max-width: 767px)';

type MapEngine = typeof import('./mapLibreEngine');

type OverlayProps = Record<string, string | number | boolean | null>;

type OverlayFeature = {
  type: 'Feature';
  properties: OverlayProps;
  geometry:
    | { type: 'Point'; coordinates: [number, number] }
    | { type: 'LineString'; coordinates: [number, number][] }
    | { type: 'Polygon'; coordinates: [number, number][][] };
};

type OverlayCollection = { type: 'FeatureCollection'; features: OverlayFeature[] };

function collection(features: OverlayFeature[]): OverlayCollection {
  return { type: 'FeatureCollection', features };
}

function pointFeature(lat: number, lon: number, properties: OverlayProps): OverlayFeature {
  return {
    type: 'Feature',
    properties,
    geometry: { type: 'Point', coordinates: [lon, lat] },
  };
}

function lineFeature(latlngs: [number, number][], properties: OverlayProps): OverlayFeature {
  return {
    type: 'Feature',
    properties,
    geometry: { type: 'LineString', coordinates: latlngs.map(([lat, lon]) => [lon, lat]) },
  };
}

function polygonRing(coords: [number, number][]): [number, number][] {
  const ring = coords.map(([lat, lon]) => [lon, lat] as [number, number]);
  const first = ring[0];
  const last = ring[ring.length - 1];
  if (first && last && (first[0] !== last[0] || first[1] !== last[1])) {
    ring.push([first[0], first[1]]);
  }
  return ring;
}

/** Tooltip content as DOM text nodes: names come from config and must never be parsed as HTML. */
function tooltipContent(lines: string[]): HTMLElement {
  const el = document.createElement('div');
  lines.forEach((line, i) => {
    if (i > 0) el.appendChild(document.createElement('br'));
    el.appendChild(document.createTextNode(line));
  });
  return el;
}

function markerLabel(flight: FlightSummary): string {
  const primary = primaryAlert(flight.active_alerts);
  const status = primary
    ? `alert: ${formatZoneRule(primary.zone, primary.rule)}`
    : flight.is_live
      ? 'live'
      : 'archived';
  const integrity = isFlagged(flight.integrity) ? `${LIST_SEP}${integrityLabel(flight.integrity)}` : '';
  return `${displayCallsign(flight)}${LIST_SEP}${status}${integrity}`;
}

const EMPTY_HANDLE: MapViewHandle = { panToAlert: () => {}, focusOn: () => {} };

export function MapView({
  filteredFlights,
  activeFlightId,
  selectedTelemetryPoint,
  followSelectedPlane,
  zonesVisible,
  showAllPaths,
  zonesData,
  appConfig,
  pathCoords,
  pathTelemetry,
  pathAlerts,
  flights,
  drawerOpen,
  onSelectFlight,
  onFollowDisabled,
  onToggleFollow,
  onToggleZones,
  onTogglePaths,
  onToggleSidebar,
  sidebarOpen,
  sidebarId,
  mapRef,
  drawer,
  banner,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const mapInstance = useRef<MapLibreMap | null>(null);
  const engineRef = useRef<MapEngine | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const latest = useRef({
    onFollowDisabled,
    onSelectFlight,
    followSelectedPlane,
    activeFlightId,
    flights,
    drawerOpen,
  });

  useEffect(() => {
    latest.current = {
      onFollowDisabled,
      onSelectFlight,
      followSelectedPlane,
      activeFlightId,
      flights,
      drawerOpen,
    };
  });

  /** Pixels of the map hidden behind the open drawer (right side, or bottom sheet). */
  const drawerInset = (): { right: number; bottom: number } => {
    const drawerEl = rootRef.current?.querySelector<HTMLElement>('.details-drawer');
    if (!latest.current.drawerOpen || !drawerEl) return { right: 0, bottom: 0 };
    if (window.matchMedia?.(BOTTOM_SHEET_QUERY).matches) {
      return { right: 0, bottom: drawerEl.offsetHeight };
    }
    return { right: drawerEl.offsetWidth, bottom: 0 };
  };

  const cameraOffset = (): [number, number] => {
    const inset = drawerInset();
    return [-inset.right / 2, -inset.bottom / 2];
  };

  /** Pan only when the plane drifted off its spot (avoids a snap on every tick). */
  const followPlane = (map: MapLibreMap, lat: number, lon: number, animate: boolean) => {
    const inset = drawerInset();
    const container = map.getContainer();
    const desiredX = container.clientWidth / 2 - inset.right / 2;
    const desiredY = container.clientHeight / 2 - inset.bottom / 2;
    const planePt = map.project([lon, lat]);
    const dx = planePt.x - desiredX;
    const dy = planePt.y - desiredY;
    if (Math.hypot(dx, dy) < 1.5) return;
    map.panBy([dx, dy], { animate, duration: animate ? 350 : 0, essential: true });
  };

  const panInside = (map: MapLibreMap, lat: number, lon: number) => {
    const inset = drawerInset();
    const pad = { top: 24, left: 24, right: inset.right + 24, bottom: inset.bottom + 24 };
    const p = map.project([lon, lat]);
    const w = map.getContainer().clientWidth;
    const h = map.getContainer().clientHeight;
    let dx = 0;
    let dy = 0;
    if (p.x < pad.left) dx = p.x - pad.left;
    else if (p.x > w - pad.right) dx = p.x - (w - pad.right);
    if (p.y < pad.top) dy = p.y - pad.top;
    else if (p.y > h - pad.bottom) dy = p.y - (h - pad.bottom);
    if (dx || dy) map.panBy([dx, dy]);
  };

  // Map lifecycle (once).
  useEffect(() => {
    if (!containerRef.current || mapInstance.current) return undefined;
    const container = containerRef.current;
    let disposed = false;
    import('./mapLibreEngine')
      .then((engine) => {
        if (disposed || !container.isConnected) return;
        engineRef.current = engine;
        const map = engine.createMap(container);
        mapInstance.current = map;

        map.on('dragstart', () => latest.current.onFollowDisabled());
        map.on('zoomend', () => {
          const { followSelectedPlane: follow, activeFlightId: id, flights: all } = latest.current;
          if (!follow || !id) return;
          const flight = all.find((f) => f.flight_id === id);
          if (flight?.latitude == null || flight?.longitude == null) return;
          followPlane(map, flight.latitude, flight.longitude, false);
        });

        const popup = new engine.Popup({
          closeButton: false,
          closeOnClick: false,
          offset: 12,
          className: 'map-tooltip',
          maxWidth: '280px',
        });
        let hoverLabel = '';
        const showHover = (label: string, lngLat: { lng: number; lat: number }) => {
          if (label !== hoverLabel) {
            hoverLabel = label;
            popup.setDOMContent(tooltipContent(label.split('\n')));
          }
          popup.setLngLat(lngLat).addTo(map);
        };
        const hideHover = () => {
          hoverLabel = '';
          popup.remove();
        };

        map.on('load', () => {
          if (disposed) return;
          engine.addOverlayLayers(map);
          for (const layer of engine.HOVER_LAYERS) {
            map.on('mouseenter', layer, () => {
              map.getCanvas().style.cursor = 'pointer';
            });
            map.on('mouseleave', layer, () => {
              map.getCanvas().style.cursor = '';
              hideHover();
            });
            map.on('mousemove', layer, (e) => {
              const label = e.features?.[0]?.properties?.label;
              if (label == null || label === '' || !e.lngLat) return;
              showHover(String(label), e.lngLat);
            });
          }
          map.on('click', (e) => {
            const hits = map.queryRenderedFeatures(e.point, { layers: [...engine.SELECT_LAYERS] });
            const id = hits[0]?.properties?.flightId;
            if (id != null && id !== '') latest.current.onSelectFlight(String(id));
          });
          setMapReady(true);
        });

        mapRef.current = {
          panToAlert: (lat, lon) => {
            const zoom = Math.max(map.getZoom(), 14);
            map.easeTo({
              center: [lon, lat],
              zoom,
              offset: cameraOffset(),
              duration: 0,
              animate: false,
              essential: true,
            });
          },
          focusOn: (lat, lon, minZoom) => {
            const zoom = Math.max(map.getZoom(), minZoom);
            map.easeTo({
              center: [lon, lat],
              zoom,
              offset: cameraOffset(),
              duration: 0,
              animate: false,
              essential: true,
            });
          },
        };
      })
      .catch((err) => console.error('Failed to load the map', err));

    const resizeObserver = new ResizeObserver(() => mapInstance.current?.resize());
    resizeObserver.observe(container);
    return () => {
      disposed = true;
      resizeObserver.disconnect();
      mapInstance.current?.remove();
      mapInstance.current = null;
      engineRef.current = null;
      setMapReady(false);
      mapRef.current = EMPTY_HANDLE;
    };
    // followPlane/cameraOffset only read refs and the DOM.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapRef]);

  const isFirstViewReset = useRef(true);
  useEffect(() => {
    const map = mapInstance.current;
    if (!map || !appConfig || !isFirstViewReset.current) return;
    if (appConfig.home?.latitude != null && appConfig.home?.longitude != null) {
      if (!latest.current.activeFlightId) {
        map.jumpTo({ center: [appConfig.home.longitude, appConfig.home.latitude], zoom: 8 });
      }
      isFirstViewReset.current = false;
    }
  }, [appConfig, mapReady]);

  // Selected telemetry fix.
  useEffect(() => {
    const map = mapInstance.current;
    const engine = engineRef.current;
    if (!map || !engine || !mapReady) return;
    const point = selectedTelemetryPoint;
    if (point?.latitude == null || point.longitude == null) {
      engine.setSourceData(map, engine.SRC.telemetry, engine.EMPTY_FC);
      return;
    }
    engine.setSourceData(
      map,
      engine.SRC.telemetry,
      collection([
        pointFeature(point.latitude, point.longitude, {
          label: [
            formatDateTime(point.timestamp, { withSeconds: true }),
            `Altitude: ${formatAltitudeCompact(point.altitude)}`,
            `Speed: ${formatSpeedCompact(point.speed)}`,
          ].join('\n'),
        }),
      ]),
    );
    panInside(map, point.latitude, point.longitude);
    // panInside only reads refs and the DOM.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTelemetryPoint, mapReady]);

  // Zones, home, and receivers.
  useEffect(() => {
    const map = mapInstance.current;
    const engine = engineRef.current;
    if (!map || !engine || !mapReady) return;
    if (!zonesVisible || !zonesData) {
      engine.setSourceData(map, engine.SRC.zones, engine.EMPTY_FC);
      engine.setSourceData(map, engine.SRC.stations, engine.EMPTY_FC);
      return;
    }

    const zoneFeatures: OverlayFeature[] = [];
    for (const zone of zonesData.zones || []) {
      if (!zone.coordinates?.length) continue;
      const colors = zoneColorFor(zone.name, zonesData.zones);
      zoneFeatures.push({
        type: 'Feature',
        properties: { name: zone.name, fill: colors.fill, stroke: colors.stroke },
        geometry: { type: 'Polygon', coordinates: [polygonRing(zone.coordinates)] },
      });
    }
    engine.setSourceData(map, engine.SRC.zones, collection(zoneFeatures));

    const stations: OverlayFeature[] = [];
    const home = zonesData.home;
    if (home?.latitude != null && home?.longitude != null) {
      stations.push(
        pointFeature(home.latitude, home.longitude, {
          kind: 'home',
          label: 'Home (receiver location)',
        }),
      );
    }
    for (const receiver of appConfig?.receivers ?? []) {
      const at = receiver.position;
      if (at?.latitude == null || at?.longitude == null) continue;
      stations.push(
        pointFeature(at.latitude, at.longitude, {
          kind: 'receiver',
          label: `Receiver ${receiver.name}`,
        }),
      );
    }
    engine.setSourceData(map, engine.SRC.stations, collection(stations));
  }, [zonesVisible, zonesData, appConfig, mapReady]);

  // Where multilateration puts the real transmitter of a selected spoofed target.
  const activeFlight = activeFlightId ? flights.find((f) => f.flight_id === activeFlightId) : undefined;
  const emitter = activeFlight?.integrity?.emitter;
  const emitterLat = emitter?.latitude;
  const emitterLon = emitter?.longitude;
  const claimedLat = activeFlight?.latitude;
  const claimedLon = activeFlight?.longitude;
  useEffect(() => {
    const map = mapInstance.current;
    const engine = engineRef.current;
    if (!map || !engine || !mapReady) return;
    if (emitterLat == null || emitterLon == null) {
      engine.setSourceData(map, engine.SRC.emitter, engine.EMPTY_FC);
      engine.setSourceData(map, engine.SRC.emitterLine, engine.EMPTY_FC);
      return;
    }
    engine.setSourceData(
      map,
      engine.SRC.emitter,
      collection([
        pointFeature(emitterLat, emitterLon, {
          label: 'Estimated transmitter (multilateration)',
        }),
      ]),
    );
    if (claimedLat != null && claimedLon != null) {
      engine.setSourceData(
        map,
        engine.SRC.emitterLine,
        collection([
          lineFeature(
            [
              [emitterLat, emitterLon],
              [claimedLat, claimedLon],
            ],
            {},
          ),
        ]),
      );
    } else {
      engine.setSourceData(map, engine.SRC.emitterLine, engine.EMPTY_FC);
    }
  }, [emitterLat, emitterLon, claimedLat, claimedLon, mapReady]);

  // Plane markers: GeoJSON + symbol layer; heading-only changes update `icon-rotate`.
  useEffect(() => {
    const map = mapInstance.current;
    const engine = engineRef.current;
    if (!map || !engine || !mapReady) return;
    const zones = zonesData?.zones;
    const alertColors = zonesData?.alert_colors;
    const visible = new Map(filteredFlights.map((f) => [f.flight_id, f]));
    if (activeFlightId && !visible.has(activeFlightId)) {
      const selected = flights.find((f) => f.flight_id === activeFlightId);
      if (selected) visible.set(activeFlightId, selected);
    }

    const features: OverlayFeature[] = [];
    const dpr = window.devicePixelRatio || 1;
    for (const [id, flight] of visible) {
      if (flight.latitude == null || flight.longitude == null) continue;
      const selected = id === activeFlightId;
      const style = planeStyle(flight, selected, zones, alertColors);
      const icon = planeIconId(style);
      if (!map.hasImage(icon)) {
        map.addImage(icon, renderPlaneIcon(style, dpr), { pixelRatio: dpr });
      }
      features.push(
        pointFeature(flight.latitude, flight.longitude, {
          flightId: id,
          icon,
          heading: planeHeading(flight.heading),
          selected,
          z: selected ? 1 : 0,
          label: markerLabel(flight),
        }),
      );
    }
    engine.setSourceData(map, engine.SRC.planes, collection(features));

    if (followSelectedPlane && activeFlightId) {
      const followed = visible.get(activeFlightId);
      if (followed?.latitude != null && followed?.longitude != null) {
        followPlane(map, followed.latitude, followed.longitude, true);
      }
    }
    // followPlane only reads refs and the DOM.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filteredFlights, flights, activeFlightId, followSelectedPlane, zonesData, mapReady]);

  // Paths and alert overlays.
  useEffect(() => {
    const map = mapInstance.current;
    const engine = engineRef.current;
    if (!map || !engine || !mapReady) return;
    const zones = zonesData?.zones;
    const alertColors = zonesData?.alert_colors;
    const flightById = new Map(flights.map((f) => [f.flight_id, f]));
    const visibleIds = new Set<string>(showAllPaths ? filteredFlights.map((f) => f.flight_id) : []);
    if (activeFlightId) visibleIds.add(activeFlightId);

    const pathFeatures: OverlayFeature[] = [];
    const alertFeatures: OverlayFeature[] = [];
    for (const id of visibleIds) {
      const coords = pathCoords[id];
      if (!coords || coords.length < 2) continue;
      const flight = flightById.get(id);
      const selected = id === activeFlightId;
      const style = pathStyleForFlight(flight, selected, zones, alertColors);
      pathFeatures.push(
        lineFeature(coords, {
          flightId: id,
          color: style.color,
          weight: style.weight,
          opacity: style.opacity,
        }),
      );

      const telemetry = pathTelemetry[id] ?? [];
      const flightEnd =
        flight?.end_time ??
        flight?.timestamp ??
        telemetry[telemetry.length - 1]?.timestamp ??
        Date.now() / 1000;
      for (const segment of buildAlertPathSegments(telemetry, pathAlerts[id] ?? [], flightEnd)) {
        if (segment.latlngs.length < 2) continue;
        alertFeatures.push(
          lineFeature(segment.latlngs, {
            flightId: id,
            color: alertVisual(segment, zones, alertColors).color,
            weight: selected ? 5 : 4,
            label: formatZoneRule(segment.zone, segment.rule),
          }),
        );
      }
    }
    engine.setSourceData(map, engine.SRC.paths, collection(pathFeatures));
    engine.setSourceData(map, engine.SRC.pathAlerts, collection(alertFeatures));
  }, [
    pathCoords,
    pathTelemetry,
    pathAlerts,
    showAllPaths,
    filteredFlights,
    activeFlightId,
    flights,
    zonesData,
    mapReady,
  ]);

  return (
    <div className="map-container" ref={rootRef}>
      <div className="map-overlay">
        <MapToolbar
          followVisible={!!activeFlightId}
          followActive={followSelectedPlane}
          zonesActive={zonesVisible}
          pathsActive={showAllPaths}
          onToggleFollow={onToggleFollow}
          onToggleZones={onToggleZones}
          onTogglePaths={onTogglePaths}
          onZoomIn={() => mapInstance.current?.zoomIn()}
          onZoomOut={() => mapInstance.current?.zoomOut()}
          onToggleSidebar={onToggleSidebar}
          sidebarOpen={sidebarOpen}
          sidebarId={sidebarId}
        />
        {banner}
      </div>
      <div className="map" ref={containerRef} />
      {drawer}
    </div>
  );
}
