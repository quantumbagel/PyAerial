import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../api/client';
import { connectLiveSocket } from '../api/liveSocket';
import type { Alert, AppConfig, FlightSummary, PortalView, TelemetryPoint, ZonesData } from '../api/types';
import type { SidebarTab } from '../components/Sidebar';
import { applyTelemetryPoint, mergeLiveFlights, sortFlights } from '../utils/flightData';

const ALERTS_LIMIT = 50;

function isValidCoordinate(lat?: number | null, lon?: number | null): boolean {
  return (
    typeof lat === 'number' &&
    typeof lon === 'number' &&
    Number.isFinite(lat) &&
    Number.isFinite(lon)
  );
}

export interface AlertStateChangeEvent {
  alert: Alert;
  eventType: 'activated' | 'deactivated';
}

interface UsePortalDataOptions {
  portalView: PortalView;
  setPortalView: (view: PortalView) => void;
  activeFlightIdRef: React.RefObject<string | null>;
  showAllPathsRef: React.MutableRefObject<boolean>;
  setPathCoords: React.Dispatch<React.SetStateAction<Record<string, [number, number][]>>>;
  setPathTelemetry?: React.Dispatch<React.SetStateAction<Record<string, TelemetryPoint[]>>>;
  appendSelectedTelemetry: (points: TelemetryPoint[]) => void;
  loadFlightAlerts: (flightId: string, view: PortalView, append?: boolean) => Promise<Alert[]>;
  onAlertStateChange?: (events: AlertStateChangeEvent[]) => void;
  onNewAlerts?: (alerts: Alert[]) => void;
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
  setPathTelemetry,
  appendSelectedTelemetry,
  loadFlightAlerts,
  onAlertStateChange,
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
  const [isLoadingAlerts, setIsLoadingAlerts] = useState(true);
  const [flightsError, setFlightsError] = useState<string | null>(null);
  const [alertsError, setAlertsError] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(true);

  const hasMoreAlerts = useRef(true);
  const isFetchingAlerts = useRef(false);
  const isInitialAlertsLoad = useRef(true);
  const historyRefreshVersion = useRef(0);
  const portalViewRef = useRef<PortalView>(portalView);
  const sidebarTabRef = useRef(sidebarTab);
  const appendSelectedTelemetryRef = useRef(appendSelectedTelemetry);
  const onAlertStateChangeRef = useRef(onAlertStateChange);
  const onNewAlertsRef = useRef(onNewAlerts);
  const loadFlightAlertsRef = useRef(loadFlightAlerts);
  const setPathCoordsRef = useRef(setPathCoords);
  const setPathTelemetryRef = useRef(setPathTelemetry);

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
    onAlertStateChangeRef.current = onAlertStateChange;
  }, [onAlertStateChange]);
  useEffect(() => {
    onNewAlertsRef.current = onNewAlerts;
  }, [onNewAlerts]);
  useEffect(() => {
    loadFlightAlertsRef.current = loadFlightAlerts;
  }, [loadFlightAlerts]);
  useEffect(() => {
    setPathCoordsRef.current = setPathCoords;
  }, [setPathCoords]);
  useEffect(() => {
    setPathTelemetryRef.current = setPathTelemetry;
  }, [setPathTelemetry]);

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
    const version = ++historyRefreshVersion.current;
    try {
      const [flights, alerts] = await Promise.all([
        api.fetchFlights('history'),
        api.fetchAlerts('history', { limit: ALERTS_LIMIT }),
      ]);
      if (version !== historyRefreshVersion.current || portalViewRef.current !== 'history') {
        return;
      }
      setFlightsData(sortFlights(flights));
      if (!isFetchingAlerts.current) {
        setAlertsData(alerts);
        hasMoreAlerts.current = alerts.length >= ALERTS_LIMIT;
      }
      setFlightsError(null);
      setAlertsError(null);
    } catch (err) {
      if (version !== historyRefreshVersion.current) return;
      const message = 'Failed to load historical data.';
      console.error(message, err);
      setFlightsError(message);
      setAlertsError(message);
    } finally {
      if (version === historyRefreshVersion.current) {
        setIsLoadingFlights(false);
        setIsLoadingAlerts(false);
      }
    }
  }, []);

  const fetchHistoryAlerts = useCallback(async (append = false) => {
    if (isFetchingAlerts.current) return;
    isFetchingAlerts.current = true;
    const version = historyRefreshVersion.current;
    try {
      const skip = append ? alertsData.length : 0;
      const limit = append ? ALERTS_LIMIT : Math.max(ALERTS_LIMIT, alertsData.length);
      const data = await api.fetchAlerts('history', { limit, skip });
      if (version !== historyRefreshVersion.current || portalViewRef.current !== 'history') {
        return;
      }
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
      setAlertsError(null);
    } catch (err) {
      const message = 'Failed to load alerts.';
      console.error(message, err);
      setAlertsError(message);
    } finally {
      isFetchingAlerts.current = false;
    }
  }, [alertsData.length]);

  const switchPortalView = useCallback(
    (view: PortalView) => {
      if (view === portalView) return;
      stopDetailPoll();
      historyRefreshVersion.current += 1;
      isInitialAlertsLoad.current = true;
      setPortalView(view);
      resetSelection();
      setFlightsData([]);
      setAlertsData([]);
      resetPaths();
      setFlightsError(null);
      setAlertsError(null);
      setIsLoadingFlights(true);
      setIsLoadingAlerts(true);
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

  const fetchLiveData = useCallback(async () => {
    try {
      const [flights, alerts] = await Promise.all([
        api.fetchFlights('live'),
        api.fetchAlerts('live'),
      ]);
      if (portalViewRef.current !== 'live') return;
      setFlightsData(sortFlights(flights));
      setAlertsData(alerts);
      setFlightsError(null);
      setAlertsError(null);
    } catch (err) {
      const message = 'Failed to load live data.';
      console.error(message, err);
      setFlightsError(message);
      setAlertsError(message);
    } finally {
      setIsLoadingFlights(false);
      setIsLoadingAlerts(false);
    }
  }, []);

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
    if (portalView === 'live') {
      fetchLiveData();
    }
    return undefined;
  }, [portalView, fetchHistoryData, fetchLiveData]);

  useEffect(() => {
    return connectLiveSocket({
      onOpen: () => setWsConnected(true),
      onClose: () => setWsConnected(false),
      onMessage: (message) => {
        if (portalViewRef.current !== 'live') return;
        if (message.type === 'flights') {
          setIsLoadingFlights(false);
          setFlightsData((prev) => sortFlights(mergeLiveFlights(prev, message.flights)));
          setFlightsError(null);

          setPathCoordsRef.current((prev) => {
            let next = prev;
            let updated = false;

            message.flights.forEach((f: FlightSummary) => {
              if (!f.flight_id || !isValidCoordinate(f.latitude, f.longitude)) return;
              const isTracked = showAllPathsRef.current || f.flight_id === activeFlightIdRef.current;
              if (!isTracked) return;

              const existing = next[f.flight_id] || [];
              const newCoord: [number, number] = [f.latitude!, f.longitude!];

              const last = existing[existing.length - 1];
              if (last && last[0] === newCoord[0] && last[1] === newCoord[1]) {
                return;
              }

              if (!updated) {
                next = { ...next };
                updated = true;
              }
              next[f.flight_id] = [...(next[f.flight_id] || existing), newCoord];
            });

            return next;
          });
        } else if (message.type === 'alerts') {
          setIsLoadingAlerts(false);
          setAlertsData((prev) => {
            if (isInitialAlertsLoad.current) {
              isInitialAlertsLoad.current = false;
              return message.alerts;
            }

            const prevMap = new Map(prev.map((a) => [a.alert_id, a]));
            const events: AlertStateChangeEvent[] = [];
            const newlyActivated: Alert[] = [];

            message.alerts.forEach((curr: Alert) => {
              const prevAlert = prevMap.get(curr.alert_id);
              const isCurrActive = curr.active !== false && !curr.deactivated_at;
              if (!prevAlert) {
                if (isCurrActive) {
                  events.push({ alert: curr, eventType: 'activated' });
                  newlyActivated.push(curr);
                }
              } else {
                const isPrevActive = prevAlert.active !== false && !prevAlert.deactivated_at;
                if (!isPrevActive && isCurrActive) {
                  events.push({ alert: curr, eventType: 'activated' });
                  newlyActivated.push(curr);
                } else if (isPrevActive && !isCurrActive) {
                  events.push({ alert: curr, eventType: 'deactivated' });
                }
              }
            });

            if (events.length > 0) {
              if (onAlertStateChangeRef.current) {
                onAlertStateChangeRef.current(events);
              }
              if (newlyActivated.length > 0 && onNewAlertsRef.current) {
                onNewAlertsRef.current(newlyActivated);
              }
              const activatedCount = events.filter((e) => e.eventType === 'activated').length;
              if (activatedCount > 0 && sidebarTabRef.current !== 'alerts') {
                setUnreadAlertsCount((c) => c + activatedCount);
              }
            }
            return message.alerts;
          });
          setAlertsError(null);
        } else if (message.type === 'telemetry') {
          const validPoints = message.telemetry.filter((point) =>
            isValidCoordinate(point.latitude, point.longitude),
          );
          if (validPoints.length === 0) return;

          setFlightsData((prev) => {
            let next = prev;
            validPoints.forEach((point) => {
              next = applyTelemetryPoint(next, point);
            });
            return sortFlights(next);
          });

          const flightId = activeFlightIdRef.current;
          if (flightId) {
            const selectedPoints = validPoints.filter((p) => p.flight_id === flightId);
            if (selectedPoints.length > 0) {
              appendSelectedTelemetryRef.current(selectedPoints);
              loadFlightAlertsRef.current(flightId, 'live', true);
            }
          }

          setPathCoordsRef.current((prev) => {
            let next = prev;
            let updated = false;

            validPoints.forEach((point) => {
              if (!point.flight_id) return;
              const fId = point.flight_id;
              const isTracked = showAllPathsRef.current || fId === activeFlightIdRef.current;
              if (!isTracked) return;

              const existing = next[fId] || [];
              const newCoord: [number, number] = [point.latitude!, point.longitude!];

              const last = existing[existing.length - 1];
              if (last && last[0] === newCoord[0] && last[1] === newCoord[1]) {
                return;
              }

              if (!updated) {
                next = { ...next };
                updated = true;
              }
              next[fId] = [...(next[fId] || existing), newCoord];
            });

            return next;
          });

          if (setPathTelemetryRef.current) {
            setPathTelemetryRef.current((prev) => {
              let next = prev;
              let updated = false;

              validPoints.forEach((point) => {
                if (!point.flight_id) return;
                const fId = point.flight_id;
                const isTracked = showAllPathsRef.current || fId === activeFlightIdRef.current;
                if (!isTracked) return;

                const existing = next[fId];
                if (!existing) return;

                const last = existing[existing.length - 1];
                if (last && last.timestamp === point.timestamp) {
                  return;
                }

                if (!updated) {
                  next = { ...next };
                  updated = true;
                }
                next[fId] = [...existing, point];
              });

              return next;
            });
          }
        }
      },
    });
  }, [activeFlightIdRef, showAllPathsRef]);

  const retryFlights = useCallback(() => {
    setIsLoadingFlights(true);
    setFlightsError(null);
    if (portalView === 'live') {
      fetchLiveData();
    } else {
      fetchHistoryData();
    }
  }, [portalView, fetchLiveData, fetchHistoryData]);

  const retryAlerts = useCallback(() => {
    setIsLoadingAlerts(true);
    setAlertsError(null);
    if (portalView === 'live') {
      fetchLiveData();
    } else {
      fetchHistoryAlerts();
    }
  }, [portalView, fetchLiveData, fetchHistoryAlerts]);

  return {
    flightsData,
    alertsData,
    unreadAlertsCount,
    sidebarTab,
    zonesData,
    appConfig,
    isLoadingFlights,
    isLoadingAlerts,
    flightsError,
    alertsError,
    wsConnected,
    handleSwitchSidebarTab,
    switchPortalView,
    handleAlertsScroll,
    setSidebarTab,
    retryFlights,
    retryAlerts,
  };
}
