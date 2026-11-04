import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import * as api from '../api/client';
import { describeError, errorCode, type ApiErrorCode } from '../api/errors';
import { connectLiveSocket, isLiveSocketOpen } from '../api/liveSocket';
import type { Alert, FlightDetail, PortalView, TelemetryPoint } from '../api/types';
import { mergeAlertsByEpisode } from '../utils/alertData';
import { SELECTED_POINT_CAP, insertByTimestamp, trimToCap } from '../utils/buffers';
import type { DrawerTab } from '../components/DetailsDrawer';
import type { MapViewHandle } from '../components/MapView';

interface UseFlightSelectionOptions {
  portalView: PortalView;
  mapRef: RefObject<MapViewHandle | null>;
  /** Called when an alert is selected (e.g. to reveal the Alerts sidebar tab). */
  onAlertSelected?: () => void;
}

export const FLIGHT_NOT_FOUND = 'Flight not found.';

/** Failures that mean "the socket went away", worth retrying once it is back. */
const RETRY_ON_RECONNECT = new Set<ApiErrorCode>(['disconnected', 'busy', 'timeout']);

type SelectOpts = {
  follow?: boolean;
  panToLatest?: boolean;
  keepAlert?: boolean;
  /** Runs when the server has no such flight (kept across a reconnect retry). */
  onNotFound?: () => void;
};

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
  /** The server left older points out of the selected flight's telemetry. */
  const [telemetryTruncated, setTelemetryTruncated] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState<DrawerTab>('telemetry');
  const [followSelectedPlane, setFollowSelectedPlane] = useState(false);
  const [selectedTelemetryPoint, setSelectedTelemetryPoint] = useState<TelemetryPoint | null>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  // Written where the selection changes so socket handlers that run before the
  // next render apply points to the flight that was just chosen.
  const activeFlightIdRef = useRef<string | null>(null);
  const selectionTokenRef = useRef(0);
  /** A selection cut off by a disconnect, re-run when the socket is back. */
  const retryRef = useRef<{ flightId: string; initialTab?: DrawerTab; opts?: SelectOpts } | null>(null);

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
      };
    });
  }, []);

  /** After a reconnect, backfill telemetry the selected flight produced while we were away. */
  const refreshSelectedTelemetry = useCallback(async () => {
    const flightId = activeFlightIdRef.current;
    if (!flightId) return;
    const token = selectionTokenRef.current;
    try {
      const { points, truncated } = await api.fetchTelemetryPage(flightId, portalView);
      if (token !== selectionTokenRef.current) return;
      if (points.length) appendSelectedTelemetry(points);
      if (truncated) setTelemetryTruncated(true);
    } catch {
      // the live stream will fill in
    }
  }, [appendSelectedTelemetry, portalView]);

  const selectFlight = useCallback(
    async (flightId: string, initialTab?: DrawerTab, opts?: SelectOpts) => {
      const token = ++selectionTokenRef.current;
      retryRef.current = null;
      const view = portalView;
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
      setTelemetryTruncated(false);
      if (!opts?.keepAlert) setActiveAlertId(null);
      if (initialTab) setDrawerTab(initialTab);
      try {
        const [detail, telemetry, alerts] = await Promise.all([
          api.fetchFlight(flightId, view),
          api.fetchTelemetryPage(flightId, view),
          api.fetchAlerts(view, { flightId, activeOnly: false }),
        ]);
        if (token !== selectionTokenRef.current) return;
        if (!detail) {
          setIsLoading(false);
          setSelectionError(FLIGHT_NOT_FOUND);
          opts?.onNotFound?.();
          return;
        }
        const points = sortedTelemetry(telemetry.points);
        setFlightDetail(detail);
        setTelemetryTruncated(telemetry.truncated);
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
        if (RETRY_ON_RECONNECT.has(errorCode(err)) && !isLiveSocketOpen()) {
          // Keep showing "loading": the socket's own reconnect brings it back.
          retryRef.current = { flightId, initialTab, opts };
          return;
        }
        setIsLoading(false);
        const notFound = errorCode(err) === 'not_found';
        const message = notFound ? FLIGHT_NOT_FOUND : describeError(err, 'Failed to load flight details.');
        console.error(message, err);
        setSelectionError(message);
        if (notFound) opts?.onNotFound?.();
      }
    },
    [mapRef, portalView, setActive],
  );

  const selectFlightRef = useRef(selectFlight);
  useLayoutEffect(() => {
    selectFlightRef.current = selectFlight;
  });
  useEffect(
    () =>
      connectLiveSocket({
        onOpen: () => {
          const retry = retryRef.current;
          retryRef.current = null;
          if (!retry || retry.flightId !== activeFlightIdRef.current) return;
          void selectFlightRef.current(retry.flightId, retry.initialTab, { ...retry.opts, keepAlert: true });
        },
      }),
    [],
  );

  const selectAlert = useCallback(
    async (alert: Alert) => {
      setActiveAlertId(alert.alert_id);
      onAlertSelected?.();
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
    [selectFlight, mapRef, onAlertSelected],
  );

  /** Clear the selection and close the drawer. */
  const closeDrawer = useCallback(() => {
    selectionTokenRef.current += 1;
    retryRef.current = null;
    setActive(null);
    setDrawerOpen(false);
    setActiveAlertId(null);
    setSelectedTelemetryPoint(null);
    setFollowSelectedPlane(false);
    setFlightDetail(null);
    setFlightAlerts([]);
    setFlightTelemetry([]);
    setTelemetryTruncated(false);
    setSelectionError(null);
    setIsLoading(false);
  }, [setActive]);

  return {
    activeFlightId,
    activeAlertId,
    flightDetail,
    flightAlerts,
    flightTelemetry,
    telemetryTruncated,
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
