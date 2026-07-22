import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../api/client';
import type { Alert, FlightDetail, PortalView, TelemetryPoint } from '../api/types';
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
  const flightAlertsRef = useRef(flightAlerts);
  const selectionTokenRef = useRef(0);
  const portalViewRef = useRef(portalView);

  useEffect(() => {
    activeFlightIdRef.current = activeFlightId;
  }, [activeFlightId]);
  useEffect(() => {
    flightAlertsRef.current = flightAlerts;
  }, [flightAlerts]);
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

  const loadFlightAlerts = useCallback(async (flightId: string, view: PortalView, append = false) => {
    let since = 0;
    const currentAlerts = flightAlertsRef.current;
    if (append && currentAlerts.length > 0) {
      since = Math.max(...currentAlerts.map((a) => a.activated_at || 0));
    }
    const alerts = await api.fetchAlerts(view, {
      flightId,
      since: append ? since : 0,
      activeOnly: false,
    });
    if (append) {
      setFlightAlerts((prev) => {
        const ids = new Set(prev.map((a) => a.alert_id));
        return [...prev, ...alerts.filter((a) => !ids.has(a.alert_id))];
      });
    } else {
      setFlightAlerts(alerts);
    }
    return alerts;
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
  }, []);

  const selectFlight = useCallback(
    async (flightId: string, initialTab?: DrawerTab) => {
      const token = ++selectionTokenRef.current;
      stopDetailPoll();
      setSelectionError(null);
      setIsLoading(true);
      setActiveFlightId(flightId);
      setSelectedTelemetryPoint(null);
      setFollowSelectedPlane(true);
      setDrawerOpen(true);
      setFlightDetail(null);
      setFlightAlerts([]);
      setFlightTelemetry([]);
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
        setFlightDetail(detail);
        setIsLoading(false);
        if (!initialTab) {
          setDrawerTab(alerts && alerts.length > 0 ? 'alerts' : 'telemetry');
        }
        if (detail.latitude != null && detail.longitude != null && mapRef.current?.map) {
          mapRef.current.map.setView(
            [detail.latitude, detail.longitude],
            Math.max(mapRef.current.map.getZoom(), 11),
          );
        }
        if (portalViewRef.current === 'live' && detail.is_live) {
          flightDetailsPollTimer.current = setInterval(async () => {
            if (token !== selectionTokenRef.current) return;
            try {
              const updated = await api.fetchFlight(flightId, 'live');
              if (token !== selectionTokenRef.current) return;
              setFlightDetail(updated);
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
    async (alert: Alert) => {
      setActiveAlertId(alert.alert_id);
      onSelectAlertTab();
      if (alert.latitude != null && alert.longitude != null) {
        mapRef.current?.panToAlert(alert.latitude, alert.longitude);
      }
      if (alert.flight_id) {
        await selectFlight(alert.flight_id, 'alerts');
      }
    },
    [selectFlight, mapRef, onSelectAlertTab],
  );

  const closeDrawer = useCallback(() => {
    selectionTokenRef.current += 1;
    stopDetailPoll();
    setDrawerOpen(false);
    setActiveFlightId(null);
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
    appendSelectedTelemetry,
    stopDetailPoll,
  };
}
