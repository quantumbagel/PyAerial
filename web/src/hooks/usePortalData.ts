import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../api/client';
import { connectLiveSocket } from '../api/liveSocket';
import type { Alert, AppConfig, FlightSummary, PortalView, ServerStats, TelemetryPoint, ZonesData } from '../api/types';
import type { SidebarTab } from '../components/Sidebar';
import { alertEpisodeIdentity, dedupeAlerts, mergeAlertsByEpisode } from '../utils/alertData';
import { applyTelemetryPoint, mergeLiveFlights, sortFlights } from '../utils/flightData';

const PAGE_LIMIT = 50;

function isValidCoordinate(lat?: number | null, lon?: number | null): boolean {
  return (
    typeof lat === 'number' &&
    typeof lon === 'number' &&
    Number.isFinite(lat) &&
    Number.isFinite(lon)
  );
}

interface UsePortalDataOptions {
  portalView: PortalView;
  setPortalView: (view: PortalView) => void;
  activeFlightIdRef: React.RefObject<string | null>;
  showAllPathsRef: React.MutableRefObject<boolean>;
  setPathCoords: React.Dispatch<React.SetStateAction<Record<string, [number, number][]>>>;
  setPathTelemetry?: React.Dispatch<React.SetStateAction<Record<string, TelemetryPoint[]>>>;
  appendSelectedTelemetry: (points: TelemetryPoint[]) => void;
  resetSelection: () => void;
  resetPaths: () => void;
  historyQ?: string;
  historySince?: number | null;
  historyUntil?: number | null;
}

export function usePortalData({
  portalView,
  setPortalView,
  activeFlightIdRef,
  showAllPathsRef,
  setPathCoords,
  setPathTelemetry,
  appendSelectedTelemetry,
  resetSelection,
  resetPaths,
  historyQ = '',
  historySince = null,
  historyUntil = null,
}: UsePortalDataOptions) {
  const [flightsData, setFlightsData] = useState<FlightSummary[]>([]);
  const [alertsData, setAlertsData] = useState<Alert[]>([]);
  const [serverStats, setServerStats] = useState<ServerStats | null>(null);
  const [unreadAlertsCount, setUnreadAlertsCount] = useState(0);
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>('flights');
  const [zonesData, setZonesData] = useState<ZonesData | null>(null);
  const [appConfig, setAppConfig] = useState<AppConfig | null>(null);
  const [isLoadingFlights, setIsLoadingFlights] = useState(true);
  const [isLoadingAlerts, setIsLoadingAlerts] = useState(true);
  const [flightsError, setFlightsError] = useState<string | null>(null);
  const [alertsError, setAlertsError] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(false);

  const hasMoreAlerts = useRef(true);
  const isFetchingAlerts = useRef(false);
  const isInitialAlertsLoad = useRef(true);
  const alertsFetchedCount = useRef(0);
  const hasMoreFlights = useRef(true);
  const isFetchingFlights = useRef(false);
  const flightsFetchedCount = useRef(0);
  const historyRefreshVersion = useRef(0);
  const historyFilterKey = `${historyQ}|${historySince ?? ''}|${historyUntil ?? ''}`;
  const prevHistoryFilterKey = useRef(historyFilterKey);
  const portalViewRef = useRef<PortalView>(portalView);
  const sidebarTabRef = useRef(sidebarTab);
  const appendSelectedTelemetryRef = useRef(appendSelectedTelemetry);
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

  const historyListOpts = useCallback((): api.HistoryListOpts => {
    const opts: api.HistoryListOpts = { limit: PAGE_LIMIT };
    if (historyQ) opts.q = historyQ;
    if (historySince != null) opts.since = historySince;
    if (historyUntil != null) opts.until = historyUntil;
    return opts;
  }, [historyQ, historySince, historyUntil]);

  const fetchHistoryData = useCallback(async () => {
    const version = ++historyRefreshVersion.current;
    const filter = historyListOpts();
    try {
      const [flights, alerts, stats] = await Promise.all([
        api.fetchFlights('history', filter),
        api.fetchAlerts('history', filter),
        api.fetchStats(),
      ]);
      if (version !== historyRefreshVersion.current || portalViewRef.current !== 'history') {
        return;
      }
      // Do not rewind infinite-scroll pagination. Only seed / refresh the
      // first page when the user has not loaded further pages.
      if (!isFetchingFlights.current && flightsFetchedCount.current <= PAGE_LIMIT) {
        setFlightsData(sortFlights(flights));
        flightsFetchedCount.current = flights.length;
        hasMoreFlights.current = flights.length >= PAGE_LIMIT;
      }
      if (!isFetchingAlerts.current && alertsFetchedCount.current <= PAGE_LIMIT) {
        setAlertsData(dedupeAlerts(alerts));
        alertsFetchedCount.current = alerts.length;
        hasMoreAlerts.current = alerts.length >= PAGE_LIMIT;
      }
      if (stats) setServerStats(stats);
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
  }, [historyListOpts]);

  const fetchHistoryAlerts = useCallback(async (append = false) => {
    if (isFetchingAlerts.current) return;
    isFetchingAlerts.current = true;
    const version = historyRefreshVersion.current;
    const filter = historyListOpts();
    try {
      // Paginate against the number of alerts actually fetched from the server,
      // not the deduped client-side list (whose length can differ after merging).
      const skip = append ? alertsFetchedCount.current : 0;
      const limit = append ? PAGE_LIMIT : Math.max(PAGE_LIMIT, alertsFetchedCount.current);
      const data = await api.fetchAlerts('history', { ...filter, limit, skip });
      if (version !== historyRefreshVersion.current || portalViewRef.current !== 'history') {
        return;
      }
      if (append) {
        if (data.length < PAGE_LIMIT) hasMoreAlerts.current = false;
        alertsFetchedCount.current += data.length;
        setAlertsData((prev) => dedupeAlerts([...prev, ...data]));
      } else {
        setAlertsData(dedupeAlerts(data));
        alertsFetchedCount.current = data.length;
        hasMoreAlerts.current = data.length >= PAGE_LIMIT;
      }
      setAlertsError(null);
    } catch (err) {
      const message = 'Failed to load alerts.';
      console.error(message, err);
      setAlertsError(message);
    } finally {
      isFetchingAlerts.current = false;
    }
  }, [historyListOpts]);

  const fetchHistoryFlights = useCallback(async (append = false) => {
    if (isFetchingFlights.current) return;
    isFetchingFlights.current = true;
    const version = historyRefreshVersion.current;
    const filter = historyListOpts();
    try {
      const skip = append ? flightsFetchedCount.current : 0;
      const limit = append ? PAGE_LIMIT : Math.max(PAGE_LIMIT, flightsFetchedCount.current);
      const data = await api.fetchFlights('history', { ...filter, limit, skip });
      if (version !== historyRefreshVersion.current || portalViewRef.current !== 'history') {
        return;
      }
      if (append) {
        if (data.length < PAGE_LIMIT) hasMoreFlights.current = false;
        flightsFetchedCount.current += data.length;
        setFlightsData((prev) => {
          const seen = new Set(prev.map((flight) => flight.flight_id));
          const extra = data.filter((flight) => !seen.has(flight.flight_id));
          return extra.length ? sortFlights([...prev, ...extra]) : prev;
        });
      } else {
        setFlightsData(sortFlights(data));
        flightsFetchedCount.current = data.length;
        hasMoreFlights.current = data.length >= PAGE_LIMIT;
      }
      setFlightsError(null);
    } catch (err) {
      const message = 'Failed to load flights.';
      console.error(message, err);
      setFlightsError(message);
    } finally {
      isFetchingFlights.current = false;
    }
  }, [historyListOpts]);

  const switchPortalView = useCallback(
    (view: PortalView) => {
      if (view === portalView) return;
      historyRefreshVersion.current += 1;
      isInitialAlertsLoad.current = true;
      alertsFetchedCount.current = 0;
      flightsFetchedCount.current = 0;
      hasMoreAlerts.current = true;
      hasMoreFlights.current = true;
      setUnreadAlertsCount(0);
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
    [portalView, setPortalView, resetSelection, resetPaths],
  );

  const handleAlertsScroll = useCallback(
    (el: HTMLElement) => {
      if (portalView !== 'history') return;
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 50) {
        if (hasMoreAlerts.current && !isFetchingAlerts.current) {
          fetchHistoryAlerts(true);
        }
      }
    },
    [portalView, fetchHistoryAlerts],
  );

  const handleFlightsScroll = useCallback(
    (el: HTMLElement) => {
      if (portalView !== 'history') return;
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 50) {
        if (hasMoreFlights.current && !isFetchingFlights.current) {
          fetchHistoryFlights(true);
        }
      }
    },
    [portalView, fetchHistoryFlights],
  );

  const fetchLiveData = useCallback(async () => {
    try {
      const [flights, alerts, stats] = await Promise.all([
        api.fetchFlights('live'),
        api.fetchAlerts('live', { activeOnly: false }),
        api.fetchStats(),
      ]);
      if (portalViewRef.current !== 'live') return;
      setFlightsData(sortFlights(flights));
      setAlertsData(dedupeAlerts(alerts));
      if (stats) setServerStats(stats);
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
    if (portalView !== 'history') return undefined;
    if (prevHistoryFilterKey.current !== historyFilterKey) {
      prevHistoryFilterKey.current = historyFilterKey;
      flightsFetchedCount.current = 0;
      alertsFetchedCount.current = 0;
      hasMoreFlights.current = true;
      hasMoreAlerts.current = true;
      setFlightsData([]);
      setAlertsData([]);
      setIsLoadingFlights(true);
      setIsLoadingAlerts(true);
    }
    fetchHistoryData();
    const timer = setInterval(fetchHistoryData, 10000);
    return () => clearInterval(timer);
  }, [portalView, historyFilterKey, fetchHistoryData]);

  useEffect(() => {
    return connectLiveSocket({
      onOpen: () => setWsConnected(true),
      onClose: () => setWsConnected(false),
      onMessage: (message) => {
        if (portalViewRef.current !== 'live') return;
        if (message.type === 'stats') {
          setServerStats(message.stats);
        } else if (message.type === 'flights') {
          setIsLoadingFlights(false);
          setFlightsData((prev) => sortFlights(mergeLiveFlights(prev, message.flights)));
          setFlightsError(null);

          // The live store drops alerts once their flight is no longer tracked,
          // so prune them from the client list to match.
          const trackedFlightIds = new Set(message.flights.map((f) => f.flight_id));
          setAlertsData((prev) => {
            if (prev.length === 0) return prev;
            let needsPrune = false;
            for (const alert of prev) {
              if (alert.flight_id && !trackedFlightIds.has(alert.flight_id)) {
                needsPrune = true;
                break;
              }
            }
            if (!needsPrune) return prev;
            return prev.filter(
              (alert) => !alert.flight_id || trackedFlightIds.has(alert.flight_id),
            );
          });

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
              next[f.flight_id] = [...(next[f.flight_id] || existing), newCoord].slice(
                -400,
              );
            });

            return next;
          });
        } else if (message.type === 'alerts') {
          setIsLoadingAlerts(false);
          setAlertsData((prev) => {
            const dedupedIncoming = dedupeAlerts(message.alerts);
            if (isInitialAlertsLoad.current) {
              isInitialAlertsLoad.current = false;
              return dedupedIncoming;
            }

            const prevMap = new Map(prev.map((a) => [alertEpisodeIdentity(a), a]));
            const events: { alert: Alert; eventType: 'activated' | 'deactivated' }[] = [];

            dedupedIncoming.forEach((curr: Alert) => {
              const key = alertEpisodeIdentity(curr);
              const prevAlert = prevMap.get(key);
              const isCurrActive = curr.active !== false && !curr.deactivated_at;
              if (!prevAlert) {
                if (isCurrActive) {
                  events.push({ alert: curr, eventType: 'activated' });
                }
              } else {
                const isPrevActive = prevAlert.active !== false && !prevAlert.deactivated_at;
                if (!isPrevActive && isCurrActive) {
                  events.push({ alert: curr, eventType: 'activated' });
                } else if (isPrevActive && !isCurrActive) {
                  events.push({ alert: curr, eventType: 'deactivated' });
                }
              }
            });

            if (events.length > 0) {
              const activatedCount = events.filter((e) => e.eventType === 'activated').length;
              if (activatedCount > 0 && sidebarTabRef.current !== 'alerts') {
                setUnreadAlertsCount((c) => c + activatedCount);
              }
            }
            // Merge rather than replace: the WS snapshot only covers currently
            // tracked flights, so replacement would drop ended episodes for
            // flights still being tracked (or clamp the list to the WS limit).
            return mergeAlertsByEpisode(prev, dedupedIncoming);
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
    serverStats,
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
    handleFlightsScroll,
    setSidebarTab,
    retryFlights,
    retryAlerts,
  };
}
