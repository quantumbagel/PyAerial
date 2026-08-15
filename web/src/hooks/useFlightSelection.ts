import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../api/client';
import type { Alert, FlightDetail, PortalView, TelemetryPoint } from '../api/types';
import { mergeAlertsByEpisode } from '../utils/alertData';
import type { DrawerTab } from '../components/DetailsDrawer';
import type { MapViewHandle } from '../components/MapView';

interface UseFlightSelectionOptions {
  portalView: PortalView;
  mapRef: React.RefObject<MapViewHandle>;
  fetchAndSetPathRef: React.RefObject<(flightId: string, view: PortalView) => Promise<void>>;
  clearPathsIfNeeded: () => void;
  onSelectAlertTab: () => void;
}

export function useFlightSelection({
  portalView,
  mapRef,
  fetchAndSetPathRef,
  clearPathsIfNeeded,
  onSelectAlertTab,
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

  const flightDetailsPollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const activeFlightIdRef = useRef<string | null>(null);
  const selectionTokenRef = useRef(0);
  const portalViewRef = useRef(portalView);

  useEffect(() => {
    activeFlightIdRef.current = activeFlightId;
  }, [activeFlightId]);
  useEffect(() => {
    portalViewRef.current = portalView;
  }, [portalView]);

  const stopDetailPoll = useCallback(() => {
    if (flightDetailsPollTimer.current) {
      clearInterval(flightDetailsPollTimer.current);
      flightDetailsPollTimer.current = null;
    }
  }, []);

  useEffect(() => () => stopDetailPoll(), [stopDetailPoll]);

  const loadFlightAlerts = useCallback(async (flightId: string, view: PortalView, merge = false) => {
    const token = selectionTokenRef.current;
    const alerts = await api.fetchAlerts(view, {
      flightId,
      activeOnly: false,
    });
    if (token !== selectionTokenRef.current) return alerts;
    if (merge) {
      setFlightAlerts((prev) => mergeAlertsByEpisode(prev, alerts));
    } else {
      setFlightAlerts(alerts);
    }
    return alerts;
  }, []);

  const syncFlightAlerts = useCallback((alerts: Alert[]) => {
    const flightId = activeFlightIdRef.current;
    if (!flightId) return;
    const forFlight = alerts.filter((alert) => alert.flight_id === flightId);
    if (forFlight.length === 0) return;
    setFlightAlerts((prev) => mergeAlertsByEpisode(prev, forFlight));
  }, []);

  const loadFlightTelemetry = useCallback(
    async (flightId: string, view: PortalView, append = false, token = selectionTokenRef.current) => {
      let since = 0;
      if (append && flightTelemetry.length > 0) {
        since = Math.max(...flightTelemetry.map((t) => t.timestamp || 0));
      }
      const points = await api.fetchTelemetry(flightId, view, append ? since : 0);
      if (token !== selectionTokenRef.current) return points;
      if (append) {
        setFlightTelemetry((prev) => {
          const ts = new Set(prev.map((t) => t.timestamp));
          return [...prev, ...points.filter((p) => !ts.has(p.timestamp))].sort(
            (a, b) => (a.timestamp || 0) - (b.timestamp || 0),
          );
        });
      } else {
        setFlightTelemetry(points.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0)));
      }
      return points;
    },
    [flightTelemetry],
  );

  const appendSelectedTelemetry = useCallback((points: TelemetryPoint[]) => {
    setFlightTelemetry((prev) => {
      const ts = new Set(prev.map((t) => t.timestamp));
      return [...prev, ...points.filter((p) => !ts.has(p.timestamp))].sort(
        (a, b) => (a.timestamp || 0) - (b.timestamp || 0),
      );
    });
    const latestPoint = points[points.length - 1];
    if (latestPoint) {
      setFlightDetail((prev) => {
        if (!prev) return prev;
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
    }
  }, []);

  const selectFlight = useCallback(
    async (
      flightId: string,
      initialTab?: DrawerTab,
      opts?: { follow?: boolean; panToLatest?: boolean; keepAlert?: boolean },
    ) => {
      const token = ++selectionTokenRef.current;
      const follow = opts?.follow ?? true;
      const panToLatest = opts?.panToLatest ?? true;
      stopDetailPoll();
      setSelectionError(null);
      setIsLoading(true);
      setActiveFlightId(flightId);
      setSelectedTelemetryPoint(null);
      setFollowSelectedPlane(follow);
      setDrawerOpen(true);
      setFlightDetail(null);
      setFlightAlerts([]);
      setFlightTelemetry([]);
      if (!opts?.keepAlert) {
        setActiveAlertId(null);
      }
      if (initialTab) {
        setDrawerTab(initialTab);
      }
      try {
        const [detail, , alerts] = await Promise.all([
          api.fetchFlight(flightId, portalViewRef.current),
          loadFlightTelemetry(flightId, portalViewRef.current, false, token),
          loadFlightAlerts(flightId, portalViewRef.current),
          fetchAndSetPathRef.current(flightId, portalViewRef.current),
        ]);
        if (token !== selectionTokenRef.current) return;
        if (!detail) {
          setIsLoading(false);
          setSelectionError('Flight not found.');
          return;
        }
        setFlightDetail(detail);
        setIsLoading(false);
        if (!initialTab) {
          setDrawerTab(alerts && alerts.length > 0 ? 'alerts' : 'telemetry');
        }
        if (
          panToLatest &&
          detail.latitude != null &&
          detail.longitude != null &&
          mapRef.current?.map
        ) {
          mapRef.current.map.setView(
            [detail.latitude, detail.longitude],
            Math.max(mapRef.current.map.getZoom(), 11),
          );
        }
        if (portalViewRef.current === 'live' && detail.is_live) {
          flightDetailsPollTimer.current = setInterval(async () => {
            if (token !== selectionTokenRef.current) return;
            try {
              const [updated, alerts] = await Promise.all([
                api.fetchFlight(flightId, 'live'),
                api.fetchAlerts('live', { flightId, activeOnly: false }),
              ]);
              if (token !== selectionTokenRef.current) return;
              setFlightDetail(updated);
              setFlightAlerts((prev) => mergeAlertsByEpisode(prev, alerts));
            } catch (err) {
              console.error('Failed to fetch active flight details', err);
            }
          }, 10000);
        }
      } catch (err) {
        if (token !== selectionTokenRef.current) return;
        setIsLoading(false);
        const message = 'Failed to load flight details.';
        console.error(message, err);
        setSelectionError(message);
      }
    },
    [loadFlightTelemetry, loadFlightAlerts, fetchAndSetPathRef, mapRef, stopDetailPoll],
  );

  const selectAlert = useCallback(
    async (alert: Alert, episodeKey: string) => {
      setActiveAlertId(episodeKey);
      onSelectAlertTab();
      if (alert.latitude != null && alert.longitude != null) {
        mapRef.current?.panToAlert(alert.latitude, alert.longitude);
      }
      if (alert.flight_id) {
        await selectFlight(alert.flight_id, 'alerts', {
          follow: false,
          panToLatest: false,
          keepAlert: true,
        });
      }
    },
    [selectFlight, mapRef, onSelectAlertTab],
  );

  const closeDrawer = useCallback(() => {
    selectionTokenRef.current += 1;
    stopDetailPoll();
    setDrawerOpen(false);
    setActiveFlightId(null);
    setActiveAlertId(null);
    setSelectedTelemetryPoint(null);
    setFollowSelectedPlane(false);
    setFlightDetail(null);
    setFlightAlerts([]);
    setFlightTelemetry([]);
    setSelectionError(null);
    setIsLoading(false);
    clearPathsIfNeeded();
  }, [stopDetailPoll, clearPathsIfNeeded]);

  const resetSelection = useCallback(() => {
    selectionTokenRef.current += 1;
    stopDetailPoll();
    setActiveFlightId(null);
    setActiveAlertId(null);
    setFollowSelectedPlane(false);
    setDrawerOpen(false);
    setFlightDetail(null);
    setFlightAlerts([]);
    setFlightTelemetry([]);
    setSelectedTelemetryPoint(null);
    setSelectionError(null);
    setIsLoading(false);
  }, [stopDetailPoll]);

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
    resetSelection,
    loadFlightAlerts,
    syncFlightAlerts,
    appendSelectedTelemetry,
    stopDetailPoll,
  };
}
