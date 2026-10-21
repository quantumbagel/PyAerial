import { useCallback, useRef, useState } from 'react';
import * as api from '../api/client';
import { describeError, errorCode } from '../api/errors';
import type { Alert, FlightDetail, PortalView, TelemetryPoint } from '../api/types';
import { mergeAlertsByEpisode } from '../utils/alertData';
import { SELECTED_POINT_CAP, insertByTimestamp, trimToCap } from '../utils/buffers';
import type { DrawerTab } from '../components/DetailsDrawer';
import type { MapViewHandle } from '../components/MapView';
import { useLatestRef, useStableCallback } from './useStableCallback';

interface UseFlightSelectionOptions {
  portalView: PortalView;
  mapRef: React.RefObject<MapViewHandle>;
  /** Called when an alert is selected (e.g. to reveal the Alerts sidebar tab). */
  onAlertSelected?: () => void;
}

export const FLIGHT_NOT_FOUND = 'Flight not found.';

function sortedTelemetry(points: TelemetryPoint[]): TelemetryPoint[] {
  const sorted = [...points].sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  return trimToCap(sorted, SELECTED_POINT_CAP);
}

export function useFlightSelection({
  portalView,
  mapRef,
  onAlertSelected,
}: UseFlightSelectionOptions) {
  const [activeFlightId, setActiveFlightId] = useState<string | null>(null);
  const [activeAlertId, setActiveAlertId] = useState<string | null>(null);
  const [flightDetail, setFlightDetail] = useState<FlightDetail | null>(null);
  const [flightAlerts, setFlightAlerts] = useState<Alert[]>([]);
  const [flightTelemetry, setFlightTelemetry] = useState<TelemetryPoint[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState<DrawerTab>('telemetry');
  const [followSelectedPlane, setFollowSelectedPlane] = useState(false);
  const [selectedTelemetryPoint, setSelectedTelemetryPoint] = useState<TelemetryPoint | null>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  // Read synchronously by socket handlers between renders, so it is written
  // where the selection changes rather than mirrored after commit.
  const activeFlightIdRef = useRef<string | null>(null);
  const selectionTokenRef = useRef(0);
  const portalViewRef = useLatestRef(portalView);
  const notifyAlertSelected = useStableCallback(() => onAlertSelected?.());

  const setActive = useCallback((flightId: string | null) => {
    activeFlightIdRef.current = flightId;
    setActiveFlightId(flightId);
  }, []);

  const syncFlightAlerts = useCallback((alerts: Alert[]) => {
    const flightId = activeFlightIdRef.current;
    if (!flightId) return;
    const forFlight = alerts.filter((alert) => alert.flight_id === flightId);
    if (forFlight.length === 0) return;
    setFlightAlerts((prev) => mergeAlertsByEpisode(prev, forFlight));
  }, []);

  /** Merge live points for the selected flight (in order, capped). */
  const appendSelectedTelemetry = useCallback((points: TelemetryPoint[]) => {
    const flightId = activeFlightIdRef.current;
    if (!flightId) return;
    const forFlight = points.filter((p) => !p.flight_id || p.flight_id === flightId);
    if (!forFlight.length) return;
    setFlightTelemetry((prev) => insertByTimestamp(prev, forFlight, SELECTED_POINT_CAP));
    const latestPoint = forFlight.reduce((a, b) => ((b.timestamp || 0) >= (a.timestamp || 0) ? b : a));
    setFlightDetail((prev) => {
      if (!prev) return prev;
      if (prev.flight_id && prev.flight_id !== flightId) return prev;
      const newTs = Math.max(prev.timestamp ?? 0, prev.end_time ?? 0, latestPoint.timestamp);
      return {
        ...prev,
        timestamp: newTs,
        end_time: newTs,
        latitude: latestPoint.latitude ?? prev.latitude,
        longitude: latestPoint.longitude ?? prev.longitude,
        altitude: latestPoint.altitude ?? prev.altitude,
        speed: latestPoint.speed ?? prev.speed,
        heading: latestPoint.heading ?? prev.heading,
        active_alerts: latestPoint.active_alerts ?? prev.active_alerts,
      };
    });
  }, []);

  /** After a reconnect, backfill telemetry the selected flight produced while we were away. */
  const refreshSelectedTelemetry = useCallback(async () => {
    const flightId = activeFlightIdRef.current;
    if (!flightId) return;
    const token = selectionTokenRef.current;
    try {
      const points = await api.fetchTelemetry(flightId, portalViewRef.current);
      if (token !== selectionTokenRef.current) return;
      if (points.length) appendSelectedTelemetry(points);
    } catch {
      // the live stream will fill in
    }
  }, [appendSelectedTelemetry, portalViewRef]);

  const selectFlight = useCallback(
    async (
      flightId: string,
      initialTab?: DrawerTab,
      opts?: { follow?: boolean; panToLatest?: boolean; keepAlert?: boolean },
    ) => {
      const token = ++selectionTokenRef.current;
      const view = portalViewRef.current;
      const follow = opts?.follow ?? true;
      const panToLatest = opts?.panToLatest ?? true;
      setSelectionError(null);
      setIsLoading(true);
      setActive(flightId);
      setSelectedTelemetryPoint(null);
      setFollowSelectedPlane(follow);
      setDrawerOpen(true);
      setFlightDetail(null);
      setFlightAlerts([]);
      setFlightTelemetry([]);
      if (!opts?.keepAlert) setActiveAlertId(null);
      if (initialTab) setDrawerTab(initialTab);
      try {
        const [detail, telemetry, alerts] = await Promise.all([
          api.fetchFlight(flightId, view),
          api.fetchTelemetry(flightId, view),
          api.fetchAlerts(view, { flightId, activeOnly: false }),
        ]);
        if (token !== selectionTokenRef.current) return;
        if (!detail) {
          setIsLoading(false);
          setSelectionError(FLIGHT_NOT_FOUND);
          return;
        }
        const points = sortedTelemetry(telemetry);
        setFlightDetail(detail);
        // Live points may have arrived while the fetch was in flight.
        setFlightTelemetry((prev) => insertByTimestamp(points, prev, SELECTED_POINT_CAP));
        setFlightAlerts((prev) => mergeAlertsByEpisode(alerts, prev));
        setIsLoading(false);
        if (!initialTab) setDrawerTab(alerts.length > 0 ? 'alerts' : 'telemetry');
        let lastFix: TelemetryPoint | undefined;
        for (let i = points.length - 1; i >= 0; i -= 1) {
          if (points[i].latitude != null && points[i].longitude != null) {
            lastFix = points[i];
            break;
          }
        }
        const lat = detail.latitude ?? lastFix?.latitude;
        const lon = detail.longitude ?? lastFix?.longitude;
        if (panToLatest && lat != null && lon != null) {
          mapRef.current?.focusOn(lat, lon, 11);
        }
      } catch (err) {
        if (token !== selectionTokenRef.current) return;
        setIsLoading(false);
        const message =
          errorCode(err) === 'not_found'
            ? FLIGHT_NOT_FOUND
            : describeError(err, 'Failed to load flight details.');
        console.error(message, err);
        setSelectionError(message);
      }
    },
    [mapRef, portalViewRef, setActive],
  );

  const selectAlert = useCallback(
    async (alert: Alert, episodeKey: string) => {
      setActiveAlertId(episodeKey);
      notifyAlertSelected();
      if (alert.latitude != null && alert.longitude != null) {
        mapRef.current?.panToAlert(alert.latitude, alert.longitude);
      }
      if (!alert.flight_id) return;
      if (alert.flight_id === activeFlightIdRef.current) {
        setDrawerTab('alerts');
        return;
      }
      await selectFlight(alert.flight_id, 'alerts', {
        follow: false,
        panToLatest: false,
        keepAlert: true,
      });
    },
    [selectFlight, mapRef, notifyAlertSelected],
  );

  /** Clear the selection and close the drawer. Stable identity. */
  const closeDrawer = useCallback(() => {
    selectionTokenRef.current += 1;
    setActive(null);
    setDrawerOpen(false);
    setActiveAlertId(null);
    setSelectedTelemetryPoint(null);
    setFollowSelectedPlane(false);
    setFlightDetail(null);
    setFlightAlerts([]);
    setFlightTelemetry([]);
    setSelectionError(null);
    setIsLoading(false);
  }, [setActive]);

  return {
    activeFlightId,
    activeFlightIdRef,
    activeAlertId,
    flightDetail,
    flightAlerts,
    flightTelemetry,
    drawerOpen,
    drawerTab,
    setDrawerTab,
    followSelectedPlane,
    setFollowSelectedPlane,
    selectedTelemetryPoint,
    setSelectedTelemetryPoint,
    selectionError,
    isLoading,
    selectFlight,
    selectAlert,
    closeDrawer,
    resetSelection: closeDrawer,
    syncFlightAlerts,
    appendSelectedTelemetry,
    refreshSelectedTelemetry,
  };
}
