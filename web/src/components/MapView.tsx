import { useEffect, useRef } from 'react';
import * as L from 'leaflet';
import type { Alert, FlightSummary, ZonesData, TelemetryPoint, AppConfig } from '../api/types';
import { isFlightLive } from '../utils/format';
import { formatZoneRule } from '../utils/format';
import { createPlaneIcon, pathStyleForFlight } from '../utils/planeIcon';
import { zoneColorFor } from '../utils/zoneColors';
import { COLOR_HEX } from '../utils/colors';
import { buildAlertPathSegments } from '../utils/alertPathSegments';
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
  pathTelemetry: Record<string, TelemetryPoint[]>;
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
  activeAlertCount: number;
};

function markerNeedsUpdate(existing: MarkerState | undefined, flight: FlightSummary, isSelected: boolean): boolean {
  if (!existing) return true;
  const isLive = isFlightLive(flight);
  const alertCount = flight.active_alerts?.length ?? 0;
  return (
    existing.lat !== flight.latitude ||
    existing.lon !== flight.longitude ||
    existing.heading !== flight.heading ||
    existing.selected !== isSelected ||
    existing.live !== isLive ||
    existing.activeAlertCount !== alertCount
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
  const planeAlertPaths = useRef<Record<string, L.Polyline[]>>({});
  const planeProjectionTracks = useRef<Record<string, L.Polyline>>({});
  const planeProjectionIntents = useRef<Record<string, L.Polyline>>({});
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
        const layers: L.Layer[] = [];
        const path = planePaths.current[flightId];
        if (path) layers.push(path);
        for (const segment of planeAlertPaths.current[flightId] || []) {
          layers.push(segment);
        }
        if (!layers.length) return;
        const bounds = layers[0] instanceof L.Polyline ? layers[0].getBounds() : null;
        if (!bounds) return;
        for (const layer of layers.slice(1)) {
          if (layer instanceof L.Polyline) bounds.extend(layer.getBounds());
        }
        map.fitBounds(bounds, { padding: [50, 50] });
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
      planeAlertPaths.current = {};
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

    (zonesData.zones || []).forEach((zone) => {
      const colors = zoneColorFor(zone.name, zonesData.zones);
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
        existing.setIcon(createPlaneIcon(flight.heading, isSelected, isLive, flight.active_alerts));
      } else {
        const marker = L.marker(pos, {
          icon: createPlaneIcon(flight.heading, isSelected, isLive, flight.active_alerts),
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
        activeAlertCount: flight.active_alerts?.length ?? 0,
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
        if (planeAlertPaths.current[flightId]) {
          planeAlertPaths.current[flightId].forEach((segment) => map.removeLayer(segment));
          delete planeAlertPaths.current[flightId];
        }
      }
    });

    const severityColor = (severity: string) => {
      if (severity === 'alert') return COLOR_HEX.alert;
      if (severity === 'warn') return COLOR_HEX.warn;
      return COLOR_HEX.accent;
    };

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

      if (planeAlertPaths.current[flightId]) {
        planeAlertPaths.current[flightId].forEach((segment) => map.removeLayer(segment));
      }

      const telemetry = pathTelemetry[flightId] || [];
      const alerts = pathAlerts[flightId] || [];
      const flightEnd =
        flight?.end_time ??
        flight?.timestamp ??
        telemetry[telemetry.length - 1]?.timestamp ??
        Date.now() / 1000;
      const segments = buildAlertPathSegments(telemetry, alerts, flightEnd);
      const overlayPaths = segments.map((segment) => {
        const overlay = L.polyline(segment.latlngs, {
          color: severityColor(segment.severity),
          weight: isSelected ? 5 : 4,
          opacity: 0.95,
          className: 'flight-path-alert',
        }).addTo(map);
        overlay.bindTooltip(formatZoneRule(segment.zone, segment.rule));
        overlay.on('click', () => onSelectFlightRef.current(flightId));
        return overlay;
      });
      if (overlayPaths.length) {
        planeAlertPaths.current[flightId] = overlayPaths;
      } else {
        delete planeAlertPaths.current[flightId];
      }
    });
  }, [pathCoords, pathTelemetry, pathAlerts, showAllPaths, filteredFlights, activeFlightId, flights]);

  useEffect(() => {
    const map = mapInstance.current;
    if (!map) return;

    const visibleProjectionIds = new Set<string>();
    for (const flight of filteredFlights) {
      if (!isFlightLive(flight) || !flight.portal_projection?.track_path?.length) continue;
      if (flight.flight_id === activeFlightId || showAllPaths) {
        visibleProjectionIds.add(flight.flight_id);
      }
    }

    const removeLayer = (layers: Record<string, L.Polyline>, flightId: string) => {
      const layer = layers[flightId];
      if (layer) {
        map.removeLayer(layer);
        delete layers[flightId];
      }
    };

    Object.keys(planeProjectionTracks.current).forEach((flightId) => {
      if (!visibleProjectionIds.has(flightId)) {
        removeLayer(planeProjectionTracks.current, flightId);
        removeLayer(planeProjectionIntents.current, flightId);
      }
    });

    visibleProjectionIds.forEach((flightId) => {
      const flight = flights.find((f) => f.flight_id === flightId);
      const projection = flight?.portal_projection;
      if (!projection?.track_path?.length) return;

      const trackLatLngs = projection.track_path.map(([lat, lon]) => [lat, lon] as L.LatLngExpression);
      const trackStyle: L.PolylineOptions = {
        color: COLOR_HEX.accent,
        weight: flightId === activeFlightId ? 3 : 2,
        opacity: 0.8,
        dashArray: '10 8',
        className: 'flight-projection-track',
      };
      if (planeProjectionTracks.current[flightId]) {
        planeProjectionTracks.current[flightId].setLatLngs(trackLatLngs);
        planeProjectionTracks.current[flightId].setStyle(trackStyle);
      } else {
        const line = L.polyline(trackLatLngs, trackStyle).addTo(map);
        line.bindTooltip('Projected track (server)');
        planeProjectionTracks.current[flightId] = line;
      }

      const intent = projection.intent_path;
      if (intent && intent.length >= 2) {
        const intentLatLngs = intent.map(([lat, lon]) => [lat, lon] as L.LatLngExpression);
        const intentStyle: L.PolylineOptions = {
          color: '#22d3ee',
          weight: flightId === activeFlightId ? 3 : 2,
          opacity: 0.9,
          dashArray: '4 8',
          className: 'flight-projection-intent',
        };
        if (planeProjectionIntents.current[flightId]) {
          planeProjectionIntents.current[flightId].setLatLngs(intentLatLngs);
          planeProjectionIntents.current[flightId].setStyle(intentStyle);
        } else {
          const line = L.polyline(intentLatLngs, intentStyle).addTo(map);
          line.bindTooltip('ADS-B TC 29 selected heading');
          planeProjectionIntents.current[flightId] = line;
        }
      } else {
        removeLayer(planeProjectionIntents.current, flightId);
      }
    });
  }, [filteredFlights, flights, activeFlightId, showAllPaths]);

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
