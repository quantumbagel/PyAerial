import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { PortalView } from '../api/types';
import type { MapViewHandle } from '../components/MapView';
import type { SidebarTab } from '../components/Sidebar';
import { ALERT_SORT_STORE } from '../utils/alertData';
import { isEngineLive } from '../utils/emptyStates';
import { FLIGHT_SORT_STORE, sortFlightsBy } from '../utils/flightData';
import { localDayEndSeconds, localDayStartSeconds } from '../utils/historyFilters';
import { readStored, writeStored } from '../utils/storage';
import { useBootstrap } from './useBootstrap';
import { useFlightPaths } from './useFlightPaths';
import { FLIGHT_NOT_FOUND, useFlightSelection } from './useFlightSelection';
import { useHistoryFeed } from './useHistoryFeed';
import { useLiveFeed } from './useLiveFeed';
import { useSortPreference } from './useSortPreference';
import { useStableCallback } from './useStableCallback';

function initialPortalView(): PortalView {
  const view = new URLSearchParams(window.location.search).get('view');
  if (view === 'live' || view === 'history') return view;
  const saved = readStored('portalView');
  return saved === 'live' || saved === 'history' ? saved : 'live';
}

const EMPTY_MAP_HANDLE: MapViewHandle = {
  map: null,
  panToAlert: () => {},
  focusOn: () => {},
};

export function usePortalApp() {
  const [portalView, setPortalView] = useState<PortalView>(initialPortalView);
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>('flights');
  const [unreadAlertsCount, setUnreadAlertsCount] = useState(0);
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [historySinceDate, setHistorySinceDate] = useState('');
  const [historyUntilDate, setHistoryUntilDate] = useState('');
  const [zonesVisible, setZonesVisible] = useState(true);
  const flightSort = useSortPreference(portalView, FLIGHT_SORT_STORE);
  const alertSort = useSortPreference(portalView, ALERT_SORT_STORE);
  const mapRef = useRef<MapViewHandle>(EMPTY_MAP_HANDLE);

  useEffect(() => {
    writeStored('portalView', portalView);
    document.title = `PyAerial · ${portalView === 'live' ? 'Live' : 'History'}`;
  }, [portalView]);

  useEffect(() => {
    if (portalView !== 'history') {
      setDebouncedSearch(searchQuery);
      return undefined;
    }
    const timer = window.setTimeout(() => setDebouncedSearch(searchQuery), 300);
    return () => window.clearTimeout(timer);
  }, [searchQuery, portalView]);

  const switchSidebarTab = useCallback((tab: SidebarTab) => {
    setSidebarTab(tab);
    if (tab === 'alerts') setUnreadAlertsCount(0);
  }, []);

  const sidebarTabRef = useRef(sidebarTab);
  useEffect(() => {
    sidebarTabRef.current = sidebarTab;
  }, [sidebarTab]);

  const bootstrap = useBootstrap();

  const selection = useFlightSelection({
    portalView,
    mapRef,
    onAlertSelected: () => switchSidebarTab('alerts'),
  });

  const live = useLiveFeed(portalView === 'live', {
    onFlights: (flights) => paths.appendFlightPositions(flights),
    onTelemetry: (points) => {
      selection.appendSelectedTelemetry(points);
      paths.appendLiveTelemetry(points);
    },
    onAlertsActivated: (count) => {
      if (sidebarTabRef.current !== 'alerts') setUnreadAlertsCount((c) => c + count);
    },
    onReconnect: () => {
      void selection.refreshSelectedTelemetry();
      paths.refetchActivePath();
    },
  });

  const historyFilter = useMemo(
    () => ({
      q: debouncedSearch.trim(),
      since: localDayStartSeconds(historySinceDate),
      until: localDayEndSeconds(historyUntilDate),
    }),
    [debouncedSearch, historySinceDate, historyUntilDate],
  );
  const history = useHistoryFeed(portalView === 'history', historyFilter);
  const feed = portalView === 'live' ? live : history;

  const filteredFlights = useMemo(() => {
    const q = searchQuery.toLowerCase();
    // History is filtered server-side.
    const source =
      portalView === 'history' || !q
        ? feed.flights
        : feed.flights.filter((flight) => {
            const activeAlertText = (flight.active_alerts ?? [])
              .map((a) => `${a.zone} ${a.rule}`)
              .join(' ');
            return [
              flight.callsign,
              flight.icao,
              flight.model,
              flight.aircraft_type,
              flight.owner,
              flight.flight_id,
              activeAlertText,
            ].some((field) => (field || '').toLowerCase().includes(q));
          });
    return sortFlightsBy(source, flightSort.field, flightSort.direction);
  }, [feed.flights, portalView, searchQuery, flightSort.field, flightSort.direction]);

  const paths = useFlightPaths(portalView, selection.activeFlightId, filteredFlights);

  const filteredAlerts = useMemo(() => {
    const q = searchQuery.toLowerCase();
    if (portalView === 'history') return feed.alerts;
    const trackedFlightIds = new Set(feed.flights.map((f) => f.flight_id));
    const scoped = feed.alerts.filter(
      (alert) => alert.flight_id && trackedFlightIds.has(alert.flight_id),
    );
    if (!q) return scoped;
    return scoped.filter((alert) =>
      [alert.callsign, alert.icao, alert.zone, alert.rule].some((field) =>
        (field || '').toLowerCase().includes(q),
      ),
    );
  }, [feed.alerts, feed.flights, portalView, searchQuery]);

  const flightCount = useMemo(
    () =>
      portalView === 'live'
        ? feed.flights.filter((f) => f.is_live).length
        : feed.flights.length,
    [feed.flights, portalView],
  );

  const { activeFlightId, closeDrawer, setFollowSelectedPlane, syncFlightAlerts, selectFlight } =
    selection;
  const { resetPaths, mergeLiveAlerts } = paths;

  const switchPortalView = useCallback(
    (view: PortalView) => {
      if (view === portalView) return;
      setPortalView(view);
      setUnreadAlertsCount(0);
      closeDrawer();
      resetPaths();
    },
    [portalView, closeDrawer, resetPaths],
  );

  // ?flight=<id> deep links: select it, falling back from live to history.
  const pendingUrlFlight = useRef<string | null>(
    new URLSearchParams(window.location.search).get('flight'),
  );

  useEffect(() => {
    const id = pendingUrlFlight.current;
    if (!id) return;
    void selectFlight(id);
  }, [selectFlight, portalView]);

  useEffect(() => {
    const id = pendingUrlFlight.current;
    if (!id) return;
    if (selection.flightDetail && activeFlightId === id) {
      pendingUrlFlight.current = null;
      return;
    }
    if (selection.selectionError === FLIGHT_NOT_FOUND && !selection.isLoading) {
      if (portalView === 'live') {
        switchPortalView('history');
      } else {
        pendingUrlFlight.current = null;
      }
    }
  }, [
    portalView,
    switchPortalView,
    selection.flightDetail,
    activeFlightId,
    selection.selectionError,
    selection.isLoading,
  ]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (portalView === 'history') params.set('view', 'history');
    else params.delete('view');
    if (activeFlightId) params.set('flight', activeFlightId);
    else params.delete('flight');
    const qs = params.toString();
    const next = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
    window.history.replaceState(null, '', next);
  }, [portalView, activeFlightId]);

  const handleCloseDrawer = useCallback(() => {
    pendingUrlFlight.current = null;
    closeDrawer();
  }, [closeDrawer]);

  // A live flight that drops off the feed takes its drawer with it.
  useEffect(() => {
    if (portalView !== 'live' || !activeFlightId) return;
    if (feed.isLoadingFlights) return;
    if (feed.flights.some((f) => f.flight_id === activeFlightId)) return;
    if (pendingUrlFlight.current === activeFlightId) return;
    closeDrawer();
  }, [feed.flights, activeFlightId, portalView, feed.isLoadingFlights, closeDrawer]);

  useEffect(() => {
    if (portalView !== 'live') return;
    if (activeFlightId) syncFlightAlerts(feed.alerts);
    // Keep alert-path overlays in sync with newly (de)activated episodes.
    mergeLiveAlerts(feed.alerts);
  }, [feed.alerts, portalView, activeFlightId, syncFlightAlerts, mergeLiveAlerts]);

  const disableFollow = useCallback(() => setFollowSelectedPlane(false), [setFollowSelectedPlane]);

  const retryFlights = useStableCallback(() =>
    portalView === 'live' ? live.retry() : history.retry('flights'),
  );
  const retryAlerts = useStableCallback(() =>
    portalView === 'live' ? live.retry() : history.retry('alerts'),
  );
  const loadMoreFlights = useStableCallback(() => {
    if (portalView === 'history') void history.loadMore('flights');
  });
  const loadMoreAlerts = useStableCallback(() => {
    if (portalView === 'history') void history.loadMore('alerts');
  });

  return {
    portalView,
    switchPortalView,
    sidebarTab,
    switchSidebarTab,
    unreadAlertsCount,
    searchQuery,
    setSearchQuery,
    historySinceDate,
    historyUntilDate,
    setHistorySinceDate,
    setHistoryUntilDate,
    zonesVisible,
    setZonesVisible,
    mapRef,
    selection: { ...selection, closeDrawer: handleCloseDrawer },
    bootstrap,
    feed,
    paths,
    filteredFlights,
    filteredAlerts,
    flightCount,
    flightSort,
    alertSort,
    disableFollow,
    retryFlights,
    retryAlerts,
    loadMoreFlights,
    loadMoreAlerts,
    engineIdle:
      portalView === 'live' &&
      bootstrap.wsStatus === 'connected' &&
      feed.stats != null &&
      !isEngineLive(feed.stats),
  };
}
