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

  const flightDetailsPollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const activeFlightIdRef = useRef<string | null>(null);
  const flightAlertsRef = useRef(flightAlerts);

  useEffect(() => {
    activeFlightIdRef.current = activeFlightId;
  }, [activeFlightId]);
  useEffect(() => {
    flightAlertsRef.current = flightAlerts;
  }, [flightAlerts]);

  const stopDetailPoll = useCallback(() => {
    if (flightDetailsPollTimer.current) {
      clearInterval(flightDetailsPollTimer.current);
      flightDetailsPollTimer.current = null;
    }
  }, []);

  const loadFlightAlerts = useCallback(async (flightId: string, view: PortalView, append = false) => {
    let since = 0;
    const currentAlerts = flightAlertsRef.current;
    if (append && currentAlerts.length > 0) {
      since = Math.max(...currentAlerts.map((a) => a.timestamp || 0));
    }
    const alerts = await api.fetchAlerts(view, { flightId, since: append ? since : 0 });
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
    async (flightId: string, view: PortalView, append = false) => {
      let since = 0;
      if (append && flightTelemetry.length > 0) {
        since = Math.max(...flightTelemetry.map((t) => t.timestamp || 0));
      }
      const points = await api.fetchTelemetry(flightId, view, append ? since : 0);
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
      const merged = [
        ...prev,
        ...points.filter((p) => !ts.has(p.timestamp)),
      ].sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
      return merged;
    });
  }, []);

  const selectFlight = useCallback(
    async (flightId: string, initialTab?: DrawerTab) => {
      stopDetailPoll();
      setActiveFlightId(flightId);
      setSelectedTelemetryPoint(null);
      setFollowSelectedPlane(true);
      setDrawerOpen(true);
      if (initialTab) {
        setDrawerTab(initialTab);
      }
      try {
        const [detail, , alerts] = await Promise.all([
          api.fetchFlight(flightId, portalView),
          loadFlightTelemetry(flightId, portalView),
          loadFlightAlerts(flightId, portalView),
          fetchAndSetPathRef.current(flightId, portalView),
        ]);
        setFlightDetail(detail);
        if (!initialTab) {
          setDrawerTab(alerts && alerts.length > 0 ? 'alerts' : 'telemetry');
        }
        if (detail.latitude != null && detail.longitude != null && mapRef.current?.map) {
          mapRef.current.map.setView(
            [detail.latitude, detail.longitude],
            Math.max(mapRef.current.map.getZoom(), 11),
          );
        }
        if (portalView === 'live' && detail.is_live) {
          flightDetailsPollTimer.current = setInterval(async () => {
            try {
              const updated = await api.fetchFlight(flightId, 'live');
              setFlightDetail(updated);
            } catch (err) {
              console.error('Failed to fetch active flight details', err);
            }
          }, 10000);
        }
      } catch (err) {
        console.error('Failed to fetch flight details', err);
      }
    },
    [portalView, loadFlightTelemetry, loadFlightAlerts, fetchAndSetPathRef, mapRef, stopDetailPoll],
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
    stopDetailPoll();
    setDrawerOpen(false);
    setActiveFlightId(null);
    setSelectedTelemetryPoint(null);
    setFollowSelectedPlane(false);
    setFlightDetail(null);
    setFlightAlerts([]);
    setFlightTelemetry([]);
    clearPathsIfNeeded();
  }, [stopDetailPoll, clearPathsIfNeeded]);

  const resetSelection = useCallback(() => {
    stopDetailPoll();
    setActiveFlightId(null);
    setActiveAlertId(null);
    setFollowSelectedPlane(false);
    setDrawerOpen(false);
    setFlightDetail(null);
    setFlightAlerts([]);
    setFlightTelemetry([]);
    setSelectedTelemetryPoint(null);
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
    selectFlight,
    selectAlert,
    closeDrawer,
    resetSelection,
    loadFlightAlerts,
    appendSelectedTelemetry,
    stopDetailPoll,
  };
}
