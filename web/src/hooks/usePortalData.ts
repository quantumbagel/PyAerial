import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../api/client';
import { connectLiveSocket } from '../api/liveSocket';
import type { Alert, AppConfig, FlightSummary, PortalView, TelemetryPoint, ZonesData } from '../api/types';
import type { SidebarTab } from '../components/Sidebar';
import { applyTelemetryPoint, mergeLiveFlights, sortFlights } from '../utils/flightData';

const ALERTS_LIMIT = 50;

interface UsePortalDataOptions {
  portalView: PortalView;
  setPortalView: (view: PortalView) => void;
  activeFlightIdRef: React.RefObject<string | null>;
  showAllPathsRef: React.MutableRefObject<boolean>;
  setPathCoords: React.Dispatch<React.SetStateAction<Record<string, [number, number][]>>>;
  appendSelectedTelemetry: (points: TelemetryPoint[]) => void;
  loadFlightAlerts: (flightId: string, view: PortalView, append?: boolean) => Promise<Alert[]>;
  onNewAlerts: (alerts: Alert[]) => void;
  resetSelection: () => void;
  resetPaths: () => void;
  stopDetailPoll: () => void;
}

export function usePortalData({
  portalView,
  setPortalView,
  activeFlightIdRef,
  showAllPathsRef,
  setPathCoords,
  appendSelectedTelemetry,
  loadFlightAlerts,
  onNewAlerts,
  resetSelection,
  resetPaths,
  stopDetailPoll,
}: UsePortalDataOptions) {
  const [flightsData, setFlightsData] = useState<FlightSummary[]>([]);
  const [alertsData, setAlertsData] = useState<Alert[]>([]);
  const [unreadAlertsCount, setUnreadAlertsCount] = useState(0);
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>('flights');
  const [zonesData, setZonesData] = useState<ZonesData | null>(null);
  const [appConfig, setAppConfig] = useState<AppConfig | null>(null);
  const [isLoadingFlights, setIsLoadingFlights] = useState(true);

  const hasMoreAlerts = useRef(true);
  const isFetchingAlerts = useRef(false);
  const portalViewRef = useRef<PortalView>(portalView);
  const sidebarTabRef = useRef(sidebarTab);
  const appendSelectedTelemetryRef = useRef(appendSelectedTelemetry);
  const onNewAlertsRef = useRef(onNewAlerts);
  const loadFlightAlertsRef = useRef(loadFlightAlerts);
  const setPathCoordsRef = useRef(setPathCoords);

  useEffect(() => {
    portalViewRef.current = portalView;
  }, [portalView]);
  useEffect(() => {
    sidebarTabRef.current = sidebarTab;
  }, [sidebarTab]);
  useEffect(() => {
    appendSelectedTelemetryRef.current = appendSelectedTelemetry;
  }, [appendSelectedTelemetry]);
  useEffect(() => {
    onNewAlertsRef.current = onNewAlerts;
  }, [onNewAlerts]);
  useEffect(() => {
    loadFlightAlertsRef.current = loadFlightAlerts;
  }, [loadFlightAlerts]);
  useEffect(() => {
    setPathCoordsRef.current = setPathCoords;
  }, [setPathCoords]);

  const handleSwitchSidebarTab = useCallback((tab: SidebarTab) => {
    setSidebarTab(tab);
    if (tab === 'alerts') {
      setUnreadAlertsCount(0);
    }
  }, []);

  const loadZones = useCallback(async () => {
    try {
      const data = await api.fetchZones();
      setZonesData(data);
    } catch (err) {
      console.error('Failed to fetch zones', err);
    }
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      const data = await api.fetchConfig();
      setAppConfig(data);
    } catch (err) {
      console.error('Failed to fetch config', err);
    }
  }, []);

  const fetchHistoryData = useCallback(async () => {
    try {
      const [flights, alerts] = await Promise.all([
        api.fetchFlights('history'),
        api.fetchAlerts('history', { limit: ALERTS_LIMIT }),
      ]);
      setFlightsData(sortFlights(flights));
      setAlertsData(alerts);
      hasMoreAlerts.current = alerts.length >= ALERTS_LIMIT;
    } catch (err) {
      console.error('Failed to fetch history data', err);
    } finally {
      setIsLoadingFlights(false);
    }
  }, []);

  const fetchHistoryAlerts = useCallback(async (append = false) => {
    if (isFetchingAlerts.current) return;
    isFetchingAlerts.current = true;
    try {
      const skip = append ? alertsData.length : 0;
      const limit = append ? ALERTS_LIMIT : Math.max(ALERTS_LIMIT, alertsData.length);
      const data = await api.fetchAlerts('history', { limit, skip });
      if (append) {
        if (data.length < ALERTS_LIMIT) hasMoreAlerts.current = false;
        setAlertsData((prev) => {
          const ids = new Set(prev.map((a) => a.alert_id));
          return [...prev, ...data.filter((a) => !ids.has(a.alert_id))];
        });
      } else {
        setAlertsData(data);
        hasMoreAlerts.current = data.length >= ALERTS_LIMIT;
      }
    } catch (err) {
      console.error('Failed to fetch alerts', err);
    } finally {
      isFetchingAlerts.current = false;
    }
  }, [alertsData.length]);

  const switchPortalView = useCallback(
    (view: PortalView) => {
      if (view === portalView) return;
      stopDetailPoll();
      setPortalView(view);
      resetSelection();
      setFlightsData([]);
      setAlertsData([]);
      resetPaths();
      setIsLoadingFlights(true);
    },
    [portalView, setPortalView, stopDetailPoll, resetSelection, resetPaths],
  );

  const handleAlertsScroll = useCallback(
    (el: HTMLDivElement) => {
      if (portalView !== 'history') return;
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 50) {
        if (hasMoreAlerts.current && !isFetchingAlerts.current) {
          fetchHistoryAlerts(true);
        }
      }
    },
    [portalView, fetchHistoryAlerts],
  );

  useEffect(() => {
    loadConfig();
    loadZones();
  }, [loadConfig, loadZones]);

  useEffect(() => {
    if (portalView === 'history') {
      fetchHistoryData();
      const timer = setInterval(fetchHistoryData, 10000);
      return () => clearInterval(timer);
    }
    return undefined;
  }, [portalView, fetchHistoryData]);

  useEffect(() => {
    return connectLiveSocket({
      onMessage: (message) => {
        if (portalViewRef.current !== 'live') return;
        if (message.type === 'flights') {
          setIsLoadingFlights(false);
          setFlightsData((prev) => sortFlights(mergeLiveFlights(prev, message.flights)));
        } else if (message.type === 'alerts') {
          setAlertsData((prev) => {
            const prevIds = new Set(prev.map((a) => a.alert_id));
            const newAlerts = message.alerts.filter((a: Alert) => !prevIds.has(a.alert_id));
            if (newAlerts.length > 0) {
              onNewAlertsRef.current(newAlerts);
              if (sidebarTabRef.current !== 'alerts') {
                setUnreadAlertsCount((c) => c + newAlerts.length);
              }
            }
            return message.alerts;
          });
        } else if (message.type === 'telemetry') {
          setFlightsData((prev) => {
            let next = prev;
            message.telemetry.forEach((point) => {
              next = applyTelemetryPoint(next, point);
            });
            return sortFlights(next);
          });
          const flightId = activeFlightIdRef.current;
          if (flightId) {
            const selectedPoints = message.telemetry.filter((p) => p.flight_id === flightId);
            if (selectedPoints.length > 0) {
              appendSelectedTelemetryRef.current(selectedPoints);
              setPathCoordsRef.current((prev) => {
                const existing = prev[flightId] || [];
                const added = selectedPoints
                  .filter((p) => p.latitude != null && p.longitude != null)
                  .map((p) => [p.latitude!, p.longitude!] as [number, number]);
                return { ...prev, [flightId]: [...existing, ...added] };
              });
              loadFlightAlertsRef.current(flightId, 'live', true);
            }
          }
          if (showAllPathsRef.current) {
            message.telemetry.forEach((point) => {
              if (!point.flight_id) return;
              setPathCoordsRef.current((prev) => {
                if (prev[point.flight_id!]) {
                  const existing = prev[point.flight_id!];
                  return {
                    ...prev,
                    [point.flight_id!]: [
                      ...existing,
                      [point.latitude!, point.longitude!] as [number, number],
                    ],
                  };
                }
                return prev;
              });
            });
          }
        }
      },
    });
  }, []);

  return {
    flightsData,
    alertsData,
    unreadAlertsCount,
    sidebarTab,
    zonesData,
    appConfig,
    isLoadingFlights,
    handleSwitchSidebarTab,
    switchPortalView,
    handleAlertsScroll,
    setSidebarTab,
  };
}
