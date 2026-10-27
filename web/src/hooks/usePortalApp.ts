import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { FlightSummary, PortalView, TelemetryPoint } from '../api/types';
import type { MapViewHandle } from '../components/MapView';
import { ALERT_SORT_FIELDS } from '../utils/alertData';
import { statusBanner } from '../utils/emptyStates';
import { FLIGHT_SORT_FIELDS, sortFlightsBy } from '../utils/flightData';
import { localDayEndSeconds, localDayStartSeconds } from '../utils/historyFilters';
import { readStored, writeStored } from '../utils/storage';
import { useBootstrap } from './useBootstrap';
import { useFlightPaths } from './useFlightPaths';
import { FLIGHT_NOT_FOUND, useFlightSelection } from './useFlightSelection';
import { useHistoryFeed } from './useHistoryFeed';
import { useLiveFeed } from './useLiveFeed';
import { useSidebarTab } from './useSidebarTab';
import { useSortPreference } from './useSortPreference';

function initialPortalView(): PortalView {
  const view = new URLSearchParams(window.location.search).get('view');
  if (view === 'live' || view === 'history') return view;
  const saved = readStored('portalView');
  return saved === 'live' || saved === 'history' ? saved : 'live';
}

const EMPTY_MAP_HANDLE: MapViewHandle = {
  panToAlert: () => {},
  focusOn: () => {},
};

type PathLiveApi = {
  appendFlightPositions: (flights: FlightSummary[]) => void;
  appendLiveTelemetry: (points: TelemetryPoint[]) => void;
  refetchActivePath: () => void;
};

export function usePortalApp() {
  const [portalView, setPortalView] = useState<PortalView>(initialPortalView);
  const sidebar = useSidebarTab();
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [historySinceDate, setHistorySinceDate] = useState('');
  const [historyUntilDate, setHistoryUntilDate] = useState('');
  const [zonesVisible, setZonesVisible] = useState(true);
  const flightSort = useSortPreference(portalView, 'flightSort', FLIGHT_SORT_FIELDS, 'last_seen');
  const alertSort = useSortPreference(portalView, 'alertSort', ALERT_SORT_FIELDS, 'activated');
  const mapRef = useRef<MapViewHandle>(EMPTY_MAP_HANDLE);
  const pathLive = useRef<PathLiveApi>({
    appendFlightPositions: () => {},
    appendLiveTelemetry: () => {},
    refetchActivePath: () => {},
  });

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

  const bootstrap = useBootstrap();
  const { switchTab, noteActivatedAlerts, resetUnread } = sidebar;
  const showAlertsTab = useCallback(() => switchTab('alerts'), [switchTab]);

  const selection = useFlightSelection({
    portalView,
    mapRef,
    onAlertSelected: showAlertsTab,
  });

  const live = useLiveFeed(portalView === 'live', {
    onFlights: (flights) => pathLive.current.appendFlightPositions(flights),
    onTelemetry: (points) => {
      selection.appendSelectedTelemetry(points);
      pathLive.current.appendLiveTelemetry(points);
    },
    onAlertsActivated: noteActivatedAlerts,
    onReconnect: () => {
      void selection.refreshSelectedTelemetry();
      pathLive.current.refetchActivePath();
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
  const activeFeed = portalView === 'live' ? live : history;
  // A refused origin (1008) never loads anything: say so everywhere, once.
  const rejected = bootstrap.rejectedMessage;
  const feed = useMemo(
    () =>
      rejected
        ? {
            ...activeFeed,
            isLoadingFlights: false,
            isLoadingAlerts: false,
            flightsError: rejected,
            alertsError: rejected,
          }
        : activeFeed,
    [activeFeed, rejected],
  );

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
  useLayoutEffect(() => {
    pathLive.current = {
      appendFlightPositions: paths.appendFlightPositions,
      appendLiveTelemetry: paths.appendLiveTelemetry,
      refetchActivePath: paths.refetchActivePath,
    };
  });

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
  const { retry: retryLive } = live;
  const { retry: retryHistory, loadMore: loadMoreHistory } = history;

  const switchPortalView = useCallback(
    (view: PortalView) => {
      if (view === portalView) return;
      setPortalView(view);
      resetUnread();
      closeDrawer();
      resetPaths();
    },
    [portalView, resetUnread, closeDrawer, resetPaths],
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

  const retryFlights = useCallback(() => {
    if (portalView === 'live') retryLive();
    else retryHistory('flights');
  }, [portalView, retryLive, retryHistory]);
  const retryAlerts = useCallback(() => {
    if (portalView === 'live') retryLive();
    else retryHistory('alerts');
  }, [portalView, retryLive, retryHistory]);
  const loadMoreFlights = useCallback(() => {
    if (portalView === 'history') void loadMoreHistory('flights');
  }, [portalView, loadMoreHistory]);
  const loadMoreAlerts = useCallback(() => {
    if (portalView === 'history') void loadMoreHistory('alerts');
  }, [portalView, loadMoreHistory]);

  return {
    portalView,
    switchPortalView,
    sidebarTab: sidebar.tab,
    switchSidebarTab: sidebar.switchTab,
    unreadAlertsCount: sidebar.unread,
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
    retryFlights: rejected ? undefined : retryFlights,
    retryAlerts: rejected ? undefined : retryAlerts,
    loadMoreFlights,
    loadMoreAlerts,
    banner: statusBanner({
      wsStatus: bootstrap.wsStatus,
      view: portalView,
      stats: feed.stats,
      rejectedMessage: bootstrap.rejectedMessage,
      bootstrapError: bootstrap.bootstrapError,
    }),
  };
}
