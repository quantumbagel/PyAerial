import { useEffect, useRef } from 'react';
import * as L from 'leaflet';
import type { Alert, FlightSummary, ZonesData, TelemetryPoint, AppConfig } from '../api/types';
import { isFlightLive, formatAlertAltitude, formatAlertEta, normalizeAlertLevel } from '../utils/format';
import { createPlaneIcon, pathStyleForFlight, ZONE_COLORS } from '../utils/planeIcon';
import { COLOR_HEX } from '../utils/colors';
import { MapToolbar } from './MapToolbar';
import '@luomus/leaflet-smooth-wheel-zoom';

export interface MapViewHandle {
  map: L.Map | null;
  fitPathBounds: (flightId: string) => void;
  panToAlert: (lat: number, lon: number) => void;
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
  pathAlerts: Record<string, Alert[]>;
  onSelectFlight: (flightId: string) => void;
  onFollowDisabled: () => void;
  onToggleFollow: () => void;
  onToggleZones: () => void;
  onTogglePaths: () => void;
  followLabel: string;
  zonesLabel: string;
  pathsLabel: string;
  followVisible: boolean;
  followActive: boolean;
  zonesActive: boolean;
  pathsActive: boolean;
  mapRef: React.MutableRefObject<MapViewHandle>;
  drawer?: React.ReactNode;
}

type MarkerState = {
  lat: number;
  lon: number;
  heading: number | null | undefined;
  selected: boolean;
  live: boolean;
  level?: string;
};

function markerNeedsUpdate(existing: MarkerState | undefined, flight: FlightSummary, isSelected: boolean): boolean {
  if (!existing) return true;
  const isLive = isFlightLive(flight);
  return (
    existing.lat !== flight.latitude ||
    existing.lon !== flight.longitude ||
    existing.heading !== flight.heading ||
    existing.selected !== isSelected ||
    existing.live !== isLive ||
    existing.level !== flight.level
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
  pathAlerts,
  flights,
  onSelectFlight,
  onFollowDisabled,
  onToggleFollow,
  onToggleZones,
  onTogglePaths,
  followLabel,
  zonesLabel,
  pathsLabel,
  followVisible,
  followActive,
  zonesActive,
  pathsActive,
  mapRef,
  drawer,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapInstance = useRef<L.Map | null>(null);
  const planeMarkers = useRef<Record<string, L.Marker>>({});
  const markerState = useRef<Record<string, MarkerState>>({});
  const planePaths = useRef<Record<string, L.Polyline>>({});
  const planeEventMarkers = useRef<Record<string, L.CircleMarker[]>>({});
  const zoneLayers = useRef<L.Layer[]>([]);
  const selectedTelemetryMarker = useRef<L.CircleMarker | null>(null);
  const onFollowDisabledRef = useRef(onFollowDisabled);
  const onSelectFlightRef = useRef(onSelectFlight);
  const lastFollowPanRef = useRef<[number, number] | null>(null);

  onFollowDisabledRef.current = onFollowDisabled;
  onSelectFlightRef.current = onSelectFlight;

  useEffect(() => {
    if (!containerRef.current || mapInstance.current) return;
    const map = L.map(containerRef.current, {
      zoomControl: false,
      zoomSnap: 0,
      zoomDelta: 1.0,
      scrollWheelZoom: false,
      smoothWheelZoom: true,
      smoothSensitivity: 1,
    }).setView([35.727, -78.696], 8);

    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
      attribution: '&copy; OpenStreetMap &copy; CARTO',
    }).addTo(map);
    map.on('dragstart', () => onFollowDisabledRef.current());
    mapInstance.current = map;
    mapRef.current = {
      map,
      fitPathBounds: (flightId: string) => {
        const path = planePaths.current[flightId];
        if (path) map.fitBounds(path.getBounds(), { padding: [50, 50] });
      },
      panToAlert: (lat: number, lon: number) => {
        map.setView([lat, lon], Math.max(map.getZoom(), 14));
      },
    };
    const resizeObserver = new ResizeObserver(() => map.invalidateSize());
    resizeObserver.observe(containerRef.current);
    return () => {
      resizeObserver.disconnect();
      map.remove();
      mapInstance.current = null;
      planeMarkers.current = {};
      markerState.current = {};
      planePaths.current = {};
      planeEventMarkers.current = {};
      zoneLayers.current = [];
      selectedTelemetryMarker.current = null;
      mapRef.current = { map: null, fitPathBounds: () => {}, panToAlert: () => {} };
    };
  }, [mapRef]);

  const isFirstViewReset = useRef(true);
  useEffect(() => {
    const map = mapInstance.current;
    if (!map || !appConfig) return;
    if (isFirstViewReset.current && appConfig.home?.latitude != null && appConfig.home?.longitude != null) {
      map.setView([appConfig.home.latitude, appConfig.home.longitude], 8);
      isFirstViewReset.current = false;
    }
  }, [appConfig]);

  useEffect(() => {
    const map = mapInstance.current;
    if (!map) return;
    if (selectedTelemetryMarker.current) {
      map.removeLayer(selectedTelemetryMarker.current);
      selectedTelemetryMarker.current = null;
    }
    if (
      selectedTelemetryPoint &&
      selectedTelemetryPoint.latitude != null &&
      selectedTelemetryPoint.longitude != null
    ) {
      const pos: L.LatLngExpression = [
        selectedTelemetryPoint.latitude,
        selectedTelemetryPoint.longitude,
      ];
      const marker = L.circleMarker(pos, {
        radius: 7,
        color: '#ffffff',
        weight: 2,
        fillColor: COLOR_HEX.accent,
        fillOpacity: 1.0,
      }).addTo(map);
      marker.bindTooltip(
        `Time: ${new Date(selectedTelemetryPoint.timestamp * 1000).toLocaleTimeString()}<br/>Alt: ${selectedTelemetryPoint.altitude} m<br/>Speed: ${selectedTelemetryPoint.speed} km/h`,
      );
      selectedTelemetryMarker.current = marker;
      map.panTo(pos);
    }
  }, [selectedTelemetryPoint]);

  useEffect(() => {
    const map = mapInstance.current;
    if (!map) return;
    zoneLayers.current.forEach((layer) => map.removeLayer(layer));
    zoneLayers.current = [];
    if (!zonesVisible || !zonesData) return;

    const home = zonesData.home;
    if (home?.latitude != null && home?.longitude != null) {
      const homeMarker = L.circleMarker([home.latitude, home.longitude], {
        radius: 6,
        color: '#38bdf8',
        fillColor: '#38bdf8',
        fillOpacity: 0.95,
        weight: 2,
      }).addTo(map);
      homeMarker.bindTooltip('Home · Receiver / reference location');
      zoneLayers.current.push(homeMarker);
    }

    (zonesData.zones || []).forEach((zone, index) => {
      const colors = ZONE_COLORS[index % ZONE_COLORS.length];
      const polygon = L.polygon(zone.coordinates, {
        color: colors.stroke,
        fillColor: colors.fill,
        fillOpacity: 0.14,
        weight: 2,
        opacity: 0.9,
      }).addTo(map);
      polygon.bindTooltip(zone.name.toUpperCase(), {
        sticky: false,
        permanent: true,
        direction: 'center',
      });
      zoneLayers.current.push(polygon);
    });
  }, [zonesVisible, zonesData]);

  useEffect(() => {
    const map = mapInstance.current;
    if (!map) return;
    if (!followSelectedPlane) lastFollowPanRef.current = null;

    const filteredIds = new Set(filteredFlights.map((f) => f.flight_id));
    Object.keys(planeMarkers.current).forEach((flightId) => {
      if (!filteredIds.has(flightId) && flightId !== activeFlightId) {
        map.removeLayer(planeMarkers.current[flightId]);
        delete planeMarkers.current[flightId];
        delete markerState.current[flightId];
      }
    });

    filteredFlights.forEach((flight) => {
      if (flight.latitude == null || flight.longitude == null) return;
      const isSelected = flight.flight_id === activeFlightId;
      const isLive = isFlightLive(flight);
      const existingState = markerState.current[flight.flight_id];
      if (!markerNeedsUpdate(existingState, flight, isSelected)) {
        if (isSelected && followSelectedPlane) {
          const lat = flight.latitude;
          const lon = flight.longitude;
          const prev = lastFollowPanRef.current;
          if (!prev || prev[0] !== lat || prev[1] !== lon) {
            lastFollowPanRef.current = [lat, lon];
            map.panTo([lat, lon], { animate: false });
          }
        }
        return;
      }

      const pos: L.LatLngExpression = [flight.latitude, flight.longitude];
      const existing = planeMarkers.current[flight.flight_id];
      if (existing) {
        existing.setLatLng(pos);
        existing.setIcon(createPlaneIcon(flight.heading, isSelected, isLive, flight.level));
      } else {
        const marker = L.marker(pos, {
          icon: createPlaneIcon(flight.heading, isSelected, isLive, flight.level),
        }).addTo(map);
        marker.on('click', () => onSelectFlightRef.current(flight.flight_id));
        planeMarkers.current[flight.flight_id] = marker;
      }
      markerState.current[flight.flight_id] = {
        lat: flight.latitude,
        lon: flight.longitude,
        heading: flight.heading,
        selected: isSelected,
        live: isLive,
        level: flight.level,
      };

      if (isSelected && followSelectedPlane) {
        const lat = flight.latitude;
        const lon = flight.longitude;
        const prev = lastFollowPanRef.current;
        if (!prev || prev[0] !== lat || prev[1] !== lon) {
          lastFollowPanRef.current = [lat, lon];
          map.panTo(pos, { animate: false });
        }
      }
    });
  }, [filteredFlights, activeFlightId, followSelectedPlane]);

  useEffect(() => {
    const map = mapInstance.current;
    if (!map) return;

    const visiblePathIds = showAllPaths
      ? new Set(filteredFlights.map((f) => f.flight_id))
      : activeFlightId
        ? new Set([activeFlightId])
        : new Set<string>();

    Object.keys(planePaths.current).forEach((flightId) => {
      if (!visiblePathIds.has(flightId)) {
        map.removeLayer(planePaths.current[flightId]);
        delete planePaths.current[flightId];
        if (planeEventMarkers.current[flightId]) {
          planeEventMarkers.current[flightId].forEach((m) => map.removeLayer(m));
          delete planeEventMarkers.current[flightId];
        }
      }
    });

    visiblePathIds.forEach((flightId) => {
      const latlngs = pathCoords[flightId];
      if (!latlngs?.length) return;
      const flight = flights.find((f) => f.flight_id === flightId);
      const isSelected = flightId === activeFlightId;
      const style = { ...pathStyleForFlight(flight, isSelected), className: 'flight-path' };
      if (planePaths.current[flightId]) {
        planePaths.current[flightId].setLatLngs(latlngs);
        planePaths.current[flightId].setStyle(style);
      } else {
        const path = L.polyline(latlngs, style).addTo(map);
        path.on('click', () => onSelectFlightRef.current(flightId));
        planePaths.current[flightId] = path;
      }

      const alerts = pathAlerts[flightId] || [];
      if (planeEventMarkers.current[flightId]) {
        planeEventMarkers.current[flightId].forEach((m) => map.removeLayer(m));
      }
      const markers: L.CircleMarker[] = [];
      alerts.forEach((alert) => {
        if (alert.latitude == null || alert.longitude == null) return;
        const norm = normalizeAlertLevel(alert.level);
        const fillColor =
          norm === 'alert' ? COLOR_HEX.alert : norm === 'warn' ? COLOR_HEX.warn : COLOR_HEX.accent;
        const displayTag = (alert.level || norm).toUpperCase();
        const marker = L.circleMarker([alert.latitude, alert.longitude], {
          radius: 6,
          color: '#fff',
          weight: 2,
          fillColor,
          fillOpacity: 0.95,
        }).addTo(map);
        const timeStr = alert.timestamp
          ? new Date(alert.timestamp * 1000).toLocaleTimeString()
          : 'N/A';
        marker.bindTooltip(
          `<strong>${displayTag}</strong> · ${alert.zone || 'zone'}<br/>Time: ${timeStr}<br/>Alt: ${formatAlertAltitude(alert.altitude)}<br/>ETA: ${formatAlertEta(alert.eta)}`,
        );
        marker.on('click', () => onSelectFlightRef.current(flightId));
        markers.push(marker);
      });
      if (markers.length) planeEventMarkers.current[flightId] = markers;
    });
  }, [pathCoords, pathAlerts, showAllPaths, filteredFlights, activeFlightId, flights]);

  return (
    <div id="map-container">
      <MapToolbar
        followVisible={followVisible}
        followActive={followActive}
        zonesActive={zonesActive}
        pathsActive={pathsActive}
        followLabel={followLabel}
        zonesLabel={zonesLabel}
        pathsLabel={pathsLabel}
        onToggleFollow={onToggleFollow}
        onToggleZones={onToggleZones}
        onTogglePaths={onTogglePaths}
        onZoomIn={() => mapInstance.current?.zoomIn()}
        onZoomOut={() => mapInstance.current?.zoomOut()}
      />
      <div id="map" ref={containerRef} />
      {drawer}
    </div>
  );
}
