import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../api/client';
import { connectLiveSocket } from '../api/liveSocket';
import type { Alert, AppConfig, FlightSummary, PortalView, ServerStats, TelemetryPoint, WsStatus, ZonesData } from '../api/types';
import type { SidebarTab } from '../components/Sidebar';
import { alertEpisodeIdentity, dedupeAlerts, mergeAlertsByEpisode } from '../utils/alertData';
import { applyTelemetryPoint, mergeLiveFlights, sortFlights } from '../utils/flightData';
import { fetchHistoryPages } from '../utils/historyPages';

const PAGE_LIMIT = 50;
const PATH_POINT_CAP = 400;

function appendOverlayPoint<T>(
  existing: T[],
  point: T,
  cap: boolean,
): T[] {
  const next = [...existing, point];
  if (!cap || existing.length > PATH_POINT_CAP) return next;
  return next.length > PATH_POINT_CAP ? next.slice(-PATH_POINT_CAP) : next;
}

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
  const [wsStatus, setWsStatus] = useState<WsStatus>('connecting');
  const [wsRejectReason, setWsRejectReason] = useState<string | null>(null);
  const [zonesError, setZonesError] = useState<string | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const bootstrapError = wsRejectReason || zonesError || configError;

  const hasMoreAlerts = useRef(true);
  const isFetchingAlerts = useRef(false);
  const isInitialAlertsLoad = useRef(true);
  const alertsFetchedCount = useRef(0);
  const hasMoreFlights = useRef(true);
  const isFetchingFlights = useRef(false);
  const flightsFetchedCount = useRef(0);
  const historyRefreshVersion = useRef(0);
  const liveRefreshVersion = useRef(0);
  const historyFilterKey = `${historyQ}|${historySince ?? ''}|${historyUntil ?? ''}`;
  const prevHistoryFilterKey = useRef(historyFilterKey);
  const portalViewRef = useRef<PortalView>(portalView);
  portalViewRef.current = portalView;
  const sidebarTabRef = useRef(sidebarTab);
  const appendSelectedTelemetryRef = useRef(appendSelectedTelemetry);
  const setPathCoordsRef = useRef(setPathCoords);
  const setPathTelemetryRef = useRef(setPathTelemetry);

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
      setZonesError(null);
    } catch (err) {
      console.error('Failed to fetch zones', err);
      setZonesError('Could not load zones.');
    }
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      const data = await api.fetchConfig();
      setAppConfig(data);
      setConfigError(null);
    } catch (err) {
      console.error('Failed to fetch config', err);
      setConfigError('Could not load station config.');
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
    if (isFetchingFlights.current || isFetchingAlerts.current) return;
    isFetchingFlights.current = true;
    isFetchingAlerts.current = true;
    const version = ++historyRefreshVersion.current;
    const filter = historyListOpts();
    try {
      const stillCurrent = () =>
        version === historyRefreshVersion.current && portalViewRef.current === 'history';
      const flightWant = Math.max(PAGE_LIMIT, flightsFetchedCount.current || PAGE_LIMIT);
      const alertWant = Math.max(PAGE_LIMIT, alertsFetchedCount.current || PAGE_LIMIT);
      const [flightPage, alertPage, stats] = await Promise.all([
        fetchHistoryPages(
          flightWant,
          (skip, limit) => api.fetchFlights('history', { ...filter, limit, skip }),
          stillCurrent,
        ),
        fetchHistoryPages(
          alertWant,
          (skip, limit) => api.fetchAlerts('history', { ...filter, limit, skip }),
          stillCurrent,
        ),
        api.fetchStats(),
      ]);
      if (!stillCurrent() || !flightPage || !alertPage) {
        return;
      }
      setFlightsData(sortFlights(flightPage.items));
      flightsFetchedCount.current = flightPage.items.length;
      hasMoreFlights.current = flightPage.hasMore;
      setAlertsData(dedupeAlerts(alertPage.items));
      alertsFetchedCount.current = alertPage.items.length;
      hasMoreAlerts.current = alertPage.hasMore;
      if (stats) setServerStats(stats);
      setFlightsError(null);
      setAlertsError(null);
    } catch (err) {
      if (version !== historyRefreshVersion.current || portalViewRef.current !== 'history') {
        return;
      }
      const message = 'Failed to load historical data.';
      console.error(message, err);
      setFlightsError(message);
      setAlertsError(message);
    } finally {
      isFetchingFlights.current = false;
      isFetchingAlerts.current = false;
      if (
        version === historyRefreshVersion.current &&
        portalViewRef.current === 'history'
      ) {
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
      if (append) {
        const data = await api.fetchAlerts('history', {
          ...filter,
          limit: PAGE_LIMIT,
          skip: alertsFetchedCount.current,
        });
        if (version !== historyRefreshVersion.current || portalViewRef.current !== 'history') {
          return;
        }
        if (data.length < PAGE_LIMIT) hasMoreAlerts.current = false;
        alertsFetchedCount.current += data.length;
        setAlertsData((prev) => dedupeAlerts([...prev, ...data]));
      } else {
        const want = Math.max(PAGE_LIMIT, alertsFetchedCount.current || PAGE_LIMIT);
        const page = await fetchHistoryPages(
          want,
          (skip, limit) => api.fetchAlerts('history', { ...filter, limit, skip }),
          () =>
            version === historyRefreshVersion.current && portalViewRef.current === 'history',
        );
        if (!page) return;
        setAlertsData(dedupeAlerts(page.items));
        alertsFetchedCount.current = page.items.length;
        hasMoreAlerts.current = page.hasMore;
      }
      setAlertsError(null);
    } catch (err) {
      if (version !== historyRefreshVersion.current || portalViewRef.current !== 'history') {
        return;
      }
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
      if (append) {
        const data = await api.fetchFlights('history', {
          ...filter,
          limit: PAGE_LIMIT,
          skip: flightsFetchedCount.current,
        });
        if (version !== historyRefreshVersion.current || portalViewRef.current !== 'history') {
          return;
        }
        if (data.length < PAGE_LIMIT) hasMoreFlights.current = false;
        flightsFetchedCount.current += data.length;
        setFlightsData((prev) => {
          const seen = new Set(prev.map((flight) => flight.flight_id));
          const extra = data.filter((flight) => !seen.has(flight.flight_id));
          return extra.length ? sortFlights([...prev, ...extra]) : prev;
        });
      } else {
        const want = Math.max(PAGE_LIMIT, flightsFetchedCount.current || PAGE_LIMIT);
        const page = await fetchHistoryPages(
          want,
          (skip, limit) => api.fetchFlights('history', { ...filter, limit, skip }),
          () =>
            version === historyRefreshVersion.current && portalViewRef.current === 'history',
        );
        if (!page) return;
        setFlightsData(sortFlights(page.items));
        flightsFetchedCount.current = page.items.length;
        hasMoreFlights.current = page.hasMore;
      }
      setFlightsError(null);
    } catch (err) {
      if (version !== historyRefreshVersion.current || portalViewRef.current !== 'history') {
        return;
      }
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
      portalViewRef.current = view;
      historyRefreshVersion.current += 1;
      liveRefreshVersion.current += 1;
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
    const version = ++liveRefreshVersion.current;
    try {
      const [flights, alerts, stats] = await Promise.all([
        api.fetchFlights('live'),
        api.fetchAlerts('live', { activeOnly: false }),
        api.fetchStats(),
      ]);
      if (version !== liveRefreshVersion.current || portalViewRef.current !== 'live') {
        return;
      }
      setFlightsData((prev) => sortFlights(mergeLiveFlights(prev, flights)));
      setAlertsData((prev) => {
        const incoming = dedupeAlerts(alerts);
        if (flights.length === 0) return incoming;
        const tracked = new Set(flights.map((flight) => flight.flight_id));
        return mergeAlertsByEpisode(prev, incoming).filter(
          (alert) => !alert.flight_id || tracked.has(alert.flight_id),
        );
      });
      if (stats) setServerStats(stats);
      setFlightsError(null);
      setAlertsError(null);
    } catch (err) {
      if (version !== liveRefreshVersion.current || portalViewRef.current !== 'live') {
        return;
      }
      const message = 'Failed to load live data.';
      console.error(message, err);
      setFlightsError(message);
      setAlertsError(message);
    } finally {
      if (version === liveRefreshVersion.current && portalViewRef.current === 'live') {
        setIsLoadingFlights(false);
        setIsLoadingAlerts(false);
      }
    }
  }, []);

  useEffect(() => {
    loadConfig();
    loadZones();
  }, [loadConfig, loadZones]);

  useEffect(() => {
    if (portalView !== 'live') return undefined;
    fetchLiveData();
    const timer = setInterval(fetchLiveData, 15000);
    return () => clearInterval(timer);
  }, [portalView, fetchLiveData]);

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
      onOpen: () => {
        setWsStatus('connected');
        setWsRejectReason(null);
        isInitialAlertsLoad.current = true;
        loadZones();
        loadConfig();
        if (portalViewRef.current === 'live') {
          fetchLiveData();
          const flightId = activeFlightIdRef.current;
          if (flightId) {
            api.fetchTelemetry(flightId, 'live').then((points) => {
              if (activeFlightIdRef.current !== flightId) return;
              if (points.length) {
                appendSelectedTelemetryRef.current(points);
              }
              const valid = points.filter((p) => isValidCoordinate(p.latitude, p.longitude));
              if (!valid.length) return;
              setPathCoordsRef.current((prev) => ({
                ...prev,
                [flightId]: valid.map((p) => [p.latitude!, p.longitude!] as [number, number]),
              }));
              setPathTelemetryRef.current?.((prev) => ({
                ...prev,
                [flightId]: valid,
              }));
            }).catch(() => {});
          }
        }
      },
      onClose: (code, reason) => {
        if (code === 1008) {
          setWsStatus('rejected');
          setWsRejectReason(reason?.trim() || 'Access denied.');
          return;
        }
        setWsStatus((prev) => (prev === 'connecting' ? 'connecting' : 'reconnecting'));
      },
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
              const cap = f.flight_id !== activeFlightIdRef.current;
              next[f.flight_id] = appendOverlayPoint(
                next[f.flight_id] || existing,
                newCoord,
                cap,
              );
            });

            return next;
          });
        } else if (message.type === 'alerts') {
          setIsLoadingAlerts(false);
          setAlertsData((prev) => {
            const dedupedIncoming = dedupeAlerts(message.alerts);
            const skipUnread = isInitialAlertsLoad.current;
            isInitialAlertsLoad.current = false;

            const prevMap = new Map(prev.map((a) => [alertEpisodeIdentity(a), a]));
            const events: { alert: Alert; eventType: 'activated' | 'deactivated' }[] = [];

            if (!skipUnread) {
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
            }
            // Merge rather than replace: the WS snapshot only covers currently
            // tracked flights, so replacement would drop ended episodes for
            // flights still being tracked (or clamp the list to the WS limit).
            return mergeAlertsByEpisode(prev, dedupedIncoming);
          });
          setAlertsError(null);
        } else if (message.type === 'telemetry') {
          const points = message.telemetry;
          if (points.length === 0) return;
          const validPoints = points.filter((point) =>
            isValidCoordinate(point.latitude, point.longitude),
          );

          setFlightsData((prev) => {
            let next = prev;
            points.forEach((point) => {
              next = applyTelemetryPoint(next, point);
            });
            return sortFlights(next);
          });

          const flightId = activeFlightIdRef.current;
          if (flightId) {
            const selectedPoints = points.filter((p) => p.flight_id === flightId);
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
              next[fId] = appendOverlayPoint(
                next[fId] || existing,
                newCoord,
                fId !== activeFlightIdRef.current,
              );
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

                const existing = next[fId] || [];
                const last = existing[existing.length - 1];
                if (last && last.timestamp === point.timestamp) {
                  return;
                }

                if (!updated) {
                  next = { ...next };
                  updated = true;
                }
                next[fId] = appendOverlayPoint(
                  existing,
                  point,
                  fId !== activeFlightIdRef.current,
                );
              });

              return next;
            });
          }
        }
      },
    });
  }, [activeFlightIdRef, showAllPathsRef, fetchLiveData]);

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
    wsStatus,
    bootstrapError,
    handleSwitchSidebarTab,
    switchPortalView,
    handleAlertsScroll,
    handleFlightsScroll,
    setSidebarTab,
    retryFlights,
    retryAlerts,
  };
}
