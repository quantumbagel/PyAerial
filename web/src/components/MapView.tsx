import { useEffect, useRef } from 'react';
import * as L from 'leaflet';
import type { Alert, AppConfig, FlightSummary, TelemetryPoint, ZonesData } from '../api/types';
import { alertVisual, primaryAlert } from '../utils/alertData';
import { buildAlertPathSegments } from '../utils/alertPathSegments';
import { PALETTE } from '../utils/colors';
import {
  LIST_SEP,
  displayCallsign,
  formatAltitudeCompact,
  formatDateTime,
  formatSpeedCompact,
  formatZoneRule,
} from '../utils/format';
import {
  createPlaneIcon,
  pathStyleForFlight,
  planeStyle,
  planeStyleKey,
  setPlaneHeading,
} from '../utils/planeIcon';
import { zoneColorFor } from '../utils/zoneColors';
import { MapToolbar } from './MapToolbar';
import '@luomus/leaflet-smooth-wheel-zoom';

export interface MapViewHandle {
  map: L.Map | null;
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

type MarkerEntry = {
  marker: L.Marker;
  lat: number;
  lon: number;
  heading: number | null | undefined;
  styleKey: string;
  label: string;
};

type OverlayInputs = {
  telemetry?: TelemetryPoint[];
  alerts?: Alert[];
  selected: boolean;
  zones: ZonesData | null;
};

type PathEntry = {
  line: L.Polyline;
  coords: [number, number][];
  styleKey: string;
  overlays: L.Polyline[];
  overlayInputs: OverlayInputs | null;
};

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
  return `${displayCallsign(flight)}${LIST_SEP}${status}`;
}

function applyMarkerLabel(marker: L.Marker, label: string) {
  const el = marker.getElement();
  if (!el) return;
  el.setAttribute('title', label);
  el.setAttribute('aria-label', label);
}

function sameInputs(a: OverlayInputs | null, b: OverlayInputs): boolean {
  return (
    !!a &&
    a.telemetry === b.telemetry &&
    a.alerts === b.alerts &&
    a.selected === b.selected &&
    a.zones === b.zones
  );
}

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
  const mapInstance = useRef<L.Map | null>(null);
  const markers = useRef(new Map<string, MarkerEntry>());
  const paths = useRef(new Map<string, PathEntry>());
  const zoneLayers = useRef<L.Layer[]>([]);
  const selectedTelemetryMarker = useRef<L.CircleMarker | null>(null);
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

  /** The map center that puts `latlng` in the middle of the unobstructed area. */
  const centerClearOfDrawer = (map: L.Map, latlng: L.LatLng, zoom: number): L.LatLng => {
    const inset = drawerInset();
    if (!inset.right && !inset.bottom) return latlng;
    return map.unproject(map.project(latlng, zoom).add([inset.right / 2, inset.bottom / 2]), zoom);
  };

  /** Pan only when the plane drifted off its spot (avoids a snap on every tick). */
  const followPlane = (map: L.Map, lat: number, lon: number, animate: boolean) => {
    const target = centerClearOfDrawer(map, L.latLng(lat, lon), map.getZoom());
    const offset = map
      .latLngToContainerPoint(target)
      .subtract(map.latLngToContainerPoint(map.getCenter()));
    if (offset.distanceTo(L.point(0, 0)) < 1.5) return;
    map.panBy(offset, { animate, duration: animate ? 0.35 : 0 });
  };

  // Map lifecycle (once).
  useEffect(() => {
    if (!containerRef.current || mapInstance.current) return undefined;
    const map = L.map(containerRef.current, {
      zoomControl: false,
      zoomSnap: 0,
      zoomDelta: 1.0,
      minZoom: 1,
      scrollWheelZoom: false,
      smoothWheelZoom: true,
      smoothSensitivity: 1,
    }).setView([35.727, -78.696], 8);
    mapInstance.current = map;

    let disposed = false;
    import('./mapLibreLayer')
      .then(({ addBasemap }) => {
        if (!disposed) addBasemap(map);
      })
      .catch((err) => console.error('Failed to load the basemap', err));

    map.on('dragstart', () => latest.current.onFollowDisabled());
    map.on('zoomend', () => {
      const { followSelectedPlane: follow, activeFlightId: id, flights: all } = latest.current;
      if (!follow || !id) return;
      const flight = all.find((f) => f.flight_id === id);
      if (flight?.latitude == null || flight?.longitude == null) return;
      followPlane(map, flight.latitude, flight.longitude, false);
    });

    mapRef.current = {
      map,
      panToAlert: (lat, lon) => {
        const zoom = Math.max(map.getZoom(), 14);
        map.setView(centerClearOfDrawer(map, L.latLng(lat, lon), zoom), zoom);
      },
      focusOn: (lat, lon, minZoom) => {
        const zoom = Math.max(map.getZoom(), minZoom);
        map.setView(centerClearOfDrawer(map, L.latLng(lat, lon), zoom), zoom);
      },
    };
    const resizeObserver = new ResizeObserver(() => map.invalidateSize());
    resizeObserver.observe(containerRef.current);
    const markerMap = markers.current;
    const pathMap = paths.current;
    return () => {
      disposed = true;
      resizeObserver.disconnect();
      map.remove();
      mapInstance.current = null;
      markerMap.clear();
      pathMap.clear();
      zoneLayers.current = [];
      selectedTelemetryMarker.current = null;
      mapRef.current = { map: null, panToAlert: () => {}, focusOn: () => {} };
    };
    // centerClearOfDrawer/followPlane only read refs and the DOM.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapRef]);

  const isFirstViewReset = useRef(true);
  useEffect(() => {
    const map = mapInstance.current;
    if (!map || !appConfig || !isFirstViewReset.current) return;
    if (appConfig.home?.latitude != null && appConfig.home?.longitude != null) {
      if (!latest.current.activeFlightId) {
        map.setView([appConfig.home.latitude, appConfig.home.longitude], 8);
      }
      isFirstViewReset.current = false;
    }
  }, [appConfig]);

  // Selected telemetry fix.
  useEffect(() => {
    const map = mapInstance.current;
    if (!map) return;
    selectedTelemetryMarker.current?.remove();
    selectedTelemetryMarker.current = null;
    const point = selectedTelemetryPoint;
    if (point?.latitude == null || point.longitude == null) return;
    const pos = L.latLng(point.latitude, point.longitude);
    const marker = L.circleMarker(pos, {
      radius: 7,
      color: PALETTE.white,
      weight: 2,
      fillColor: PALETTE.accent,
      fillOpacity: 1.0,
    }).addTo(map);
    marker.bindTooltip(
      tooltipContent([
        formatDateTime(point.timestamp, { withSeconds: true }),
        `Altitude: ${formatAltitudeCompact(point.altitude)}`,
        `Speed: ${formatSpeedCompact(point.speed)}`,
      ]),
    );
    selectedTelemetryMarker.current = marker;
    const inset = drawerInset();
    map.panInside(pos, {
      paddingTopLeft: [24, 24],
      paddingBottomRight: [inset.right + 24, inset.bottom + 24],
    });
  }, [selectedTelemetryPoint]);

  // Zones and home.
  useEffect(() => {
    const map = mapInstance.current;
    if (!map) return;
    zoneLayers.current.forEach((layer) => layer.remove());
    zoneLayers.current = [];
    if (!zonesVisible || !zonesData) return;

    const home = zonesData.home;
    if (home?.latitude != null && home?.longitude != null) {
      const homeMarker = L.circleMarker([home.latitude, home.longitude], {
        radius: 6,
        color: PALETTE.home,
        fillColor: PALETTE.home,
        fillOpacity: 0.95,
        weight: 2,
      }).addTo(map);
      homeMarker.bindTooltip(tooltipContent(['Home (receiver location)']));
      zoneLayers.current.push(homeMarker);
    }

    (zonesData.zones || []).forEach((zone) => {
      const colors = zoneColorFor(zone.name, zonesData.zones);
      const polygon = L.polygon(zone.coordinates, {
        color: colors.stroke,
        fillColor: colors.fill,
        fillOpacity: 0.14,
        weight: 2,
        opacity: 0.9,
      }).addTo(map);
      polygon.bindTooltip(tooltipContent([zone.name.toUpperCase()]), {
        permanent: true,
        direction: 'center',
        className: 'zone-label',
      });
      zoneLayers.current.push(polygon);
    });
  }, [zonesVisible, zonesData]);

  // Plane markers: diffed per flight; heading-only changes rotate in place.
  useEffect(() => {
    const map = mapInstance.current;
    if (!map) return;
    const zones = zonesData?.zones;
    const alertColors = zonesData?.alert_colors;
    const visible = new Map(filteredFlights.map((f) => [f.flight_id, f]));
    if (activeFlightId && !visible.has(activeFlightId)) {
      const selected = flights.find((f) => f.flight_id === activeFlightId);
      if (selected) visible.set(activeFlightId, selected);
    }

    for (const [id, entry] of markers.current) {
      const flight = visible.get(id);
      if (!flight || flight.latitude == null || flight.longitude == null) {
        entry.marker.remove();
        markers.current.delete(id);
      }
    }

    for (const [id, flight] of visible) {
      if (flight.latitude == null || flight.longitude == null) continue;
      const selected = id === activeFlightId;
      const style = planeStyle(flight, selected, zones, alertColors);
      const styleKey = planeStyleKey(style);
      const label = markerLabel(flight);
      const entry = markers.current.get(id);
      if (!entry) {
        const marker = L.marker([flight.latitude, flight.longitude], {
          icon: createPlaneIcon(style, flight.heading),
          title: label,
          zIndexOffset: selected ? 1000 : 0,
        }).addTo(map);
        marker.on('click', () => latest.current.onSelectFlight(id));
        applyMarkerLabel(marker, label);
        markers.current.set(id, {
          marker,
          lat: flight.latitude,
          lon: flight.longitude,
          heading: flight.heading,
          styleKey,
          label,
        });
        continue;
      }
      if (entry.lat !== flight.latitude || entry.lon !== flight.longitude) {
        entry.marker.setLatLng([flight.latitude, flight.longitude]);
        entry.lat = flight.latitude;
        entry.lon = flight.longitude;
      }
      if (entry.styleKey !== styleKey) {
        entry.marker.setIcon(createPlaneIcon(style, flight.heading));
        entry.marker.setZIndexOffset(selected ? 1000 : 0);
        applyMarkerLabel(entry.marker, label);
        entry.styleKey = styleKey;
        entry.heading = flight.heading;
        entry.label = label;
      } else if (entry.heading !== flight.heading) {
        setPlaneHeading(entry.marker, flight.heading);
        entry.heading = flight.heading;
      }
      if (entry.label !== label) {
        applyMarkerLabel(entry.marker, label);
        entry.label = label;
      }
    }

    if (followSelectedPlane && activeFlightId) {
      const followed = visible.get(activeFlightId);
      if (followed?.latitude != null && followed?.longitude != null) {
        followPlane(map, followed.latitude, followed.longitude, true);
      }
    }
    // followPlane only reads refs and the DOM.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filteredFlights, flights, activeFlightId, followSelectedPlane, zonesData]);

  // Paths and alert overlays: only flights whose inputs changed are touched.
  useEffect(() => {
    const map = mapInstance.current;
    if (!map) return;
    const zones = zonesData?.zones;
    const alertColors = zonesData?.alert_colors;
    const flightById = new Map(flights.map((f) => [f.flight_id, f]));
    const visibleIds = new Set<string>(showAllPaths ? filteredFlights.map((f) => f.flight_id) : []);
    if (activeFlightId) visibleIds.add(activeFlightId);

    for (const [id, entry] of paths.current) {
      if (visibleIds.has(id) && pathCoords[id]?.length) continue;
      entry.line.remove();
      entry.overlays.forEach((o) => o.remove());
      paths.current.delete(id);
    }

    for (const id of visibleIds) {
      const coords = pathCoords[id];
      if (!coords?.length) continue;
      const flight = flightById.get(id);
      const selected = id === activeFlightId;
      const style = {
        ...pathStyleForFlight(flight, selected, zones, alertColors),
        className: 'flight-path',
      };
      const styleKey = `${style.color}|${style.weight}|${style.opacity}`;
      let entry = paths.current.get(id);
      if (!entry) {
        const line = L.polyline(coords, style).addTo(map);
        line.on('click', () => latest.current.onSelectFlight(id));
        entry = { line, coords, styleKey, overlays: [], overlayInputs: null };
        paths.current.set(id, entry);
      } else {
        if (entry.coords !== coords) {
          entry.line.setLatLngs(coords);
          entry.coords = coords;
        }
        if (entry.styleKey !== styleKey) {
          entry.line.setStyle(style);
          entry.styleKey = styleKey;
        }
      }

      const inputs: OverlayInputs = {
        telemetry: pathTelemetry[id],
        alerts: pathAlerts[id],
        selected,
        zones: zonesData,
      };
      if (sameInputs(entry.overlayInputs, inputs)) continue;
      entry.overlays.forEach((o) => o.remove());
      const telemetry = inputs.telemetry ?? [];
      const flightEnd =
        flight?.end_time ??
        flight?.timestamp ??
        telemetry[telemetry.length - 1]?.timestamp ??
        Date.now() / 1000;
      entry.overlays = buildAlertPathSegments(telemetry, inputs.alerts ?? [], flightEnd).map(
        (segment) => {
          const overlay = L.polyline(segment.latlngs, {
            color: alertVisual(segment, zones, alertColors).color,
            weight: selected ? 5 : 4,
            opacity: 0.95,
            className: 'flight-path-alert',
          }).addTo(map);
          overlay.bindTooltip(tooltipContent([formatZoneRule(segment.zone, segment.rule)]));
          overlay.on('click', () => latest.current.onSelectFlight(id));
          return overlay;
        },
      );
      entry.overlayInputs = inputs;
    }
  }, [pathCoords, pathTelemetry, pathAlerts, showAllPaths, filteredFlights, activeFlightId, flights, zonesData]);

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
