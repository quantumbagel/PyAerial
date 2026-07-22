import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Alert, PortalView } from '../api/types';
import type { DrawerTab } from '../components/DetailsDrawer';
import type { MapViewHandle } from '../components/MapView';
import type { SidebarTab } from '../components/Sidebar';
import {
  defaultSortDirection,
  loadFlightSort,
  saveFlightSort,
  sortFlightsBy,
  type FlightSortField,
  type SortDirection,
} from '../utils/flightData';
import { useAlertNotifications } from './useAlertNotifications';
import { useFlightPaths } from './useFlightPaths';
import { useFlightSelection } from './useFlightSelection';
import { usePortalData } from './usePortalData';

export function usePortalApp() {
  const [portalView, setPortalView] = useState<PortalView>(() => {
    const saved = localStorage.getItem('portalView');
    return saved === 'live' || saved === 'history' ? saved : 'live';
  });
  const [searchQuery, setSearchQuery] = useState('');
  const [zonesVisible, setZonesVisible] = useState(true);
  const [notificationsEnabled, setNotificationsEnabled] = useState(
    () => typeof Notification !== 'undefined' && Notification.permission === 'granted',
  );
  const [flightSortField, setFlightSortField] = useState<FlightSortField>(
    () => loadFlightSort(portalView).field,
  );
  const [flightSortDirection, setFlightSortDirection] = useState<SortDirection>(
    () => loadFlightSort(portalView).direction,
  );

  useEffect(() => {
    localStorage.setItem('portalView', portalView);
  }, [portalView]);

  useEffect(() => {
    const saved = loadFlightSort(portalView);
    setFlightSortField(saved.field);
    setFlightSortDirection(saved.direction);
  }, [portalView]);

  useEffect(() => {
    saveFlightSort(portalView, flightSortField, flightSortDirection);
  }, [portalView, flightSortField, flightSortDirection]);

  const setFlightSort = useCallback((field: FlightSortField) => {
    setFlightSortField(field);
    setFlightSortDirection(defaultSortDirection(field));
  }, []);

  const toggleFlightSortDirection = useCallback(() => {
    setFlightSortDirection((d) => (d === 'asc' ? 'desc' : 'asc'));
  }, []);

  const enableNotifications = useCallback(async () => {
    if (!('Notification' in window)) return;
    const permission = await Notification.requestPermission();
    setNotificationsEnabled(permission === 'granted');
  }, []);

  const mapRef = useRef<MapViewHandle>({
    map: null,
    fitPathBounds: () => {},
    panToAlert: () => {},
  });
  const fetchAndSetPathRef = useRef<(flightId: string, view: PortalView) => Promise<void>>(
    async () => {},
  );
  const clearPathsIfNeededRef = useRef<() => void>(() => {});
  const resetPathsRef = useRef<() => void>(() => {});
  const onSelectAlertTabRef = useRef<() => void>(() => {});

  const showAllPathsRef = useRef(false);
  const setPathCoordsRef = useRef<
    React.Dispatch<React.SetStateAction<Record<string, [number, number][]>>>
  >(() => {});

  const alertNotifications = useAlertNotifications();

  const selection = useFlightSelection({
    portalView,
    mapRef,
    fetchAndSetPathRef,
    clearPathsIfNeeded: () => clearPathsIfNeededRef.current(),
    onSelectAlertTab: () => onSelectAlertTabRef.current(),
  });

  const selectAlertRef = useRef(selection.selectAlert);
  useEffect(() => {
    selectAlertRef.current = selection.selectAlert;
  }, [selection.selectAlert]);

  const onNewAlerts = useCallback(
    (newAlerts: Alert[]) => {
      const newest = newAlerts[0];
      alertNotifications.playWarningChime(newest.rule || 'warn');
      if (notificationsEnabled) {
        alertNotifications.triggerDesktopNotification(newest, (alert) =>
          selectAlertRef.current(alert),
        );
      }
      newAlerts.forEach((a) => alertNotifications.addToast(a));
    },
    [alertNotifications, notificationsEnabled],
  );

  const portal = usePortalData({
    portalView,
    setPortalView,
    activeFlightIdRef: selection.activeFlightIdRef,
    showAllPathsRef,
    setPathCoords: (update) => setPathCoordsRef.current(update),
    appendSelectedTelemetry: selection.appendSelectedTelemetry,
    loadFlightAlerts: selection.loadFlightAlerts,
    onNewAlerts,
    resetSelection: selection.resetSelection,
    resetPaths: () => resetPathsRef.current(),
    stopDetailPoll: selection.stopDetailPoll,
  });

  onSelectAlertTabRef.current = () => portal.setSidebarTab('alerts');

  const alertCountByFlight = useMemo(() => {
    const map = new Map<string, number>();
    for (const alert of portal.alertsData) {
      if (alert.flight_id) {
        map.set(alert.flight_id, (map.get(alert.flight_id) ?? 0) + 1);
      }
    }
    return map;
  }, [portal.alertsData]);

  const filteredFlights = useMemo(() => {
    const q = searchQuery.toLowerCase();
    const filtered = portal.flightsData.filter((flight) => {
      const callsign = (flight.callsign || '').toLowerCase();
      const icao = (flight.icao || '').toLowerCase();
      const model = (flight.model || '').toLowerCase();
      const aircraftType = (flight.aircraft_type || flight.typecode || '').toLowerCase();
      const activeAlertText = (flight.active_alerts ?? [])
        .map((a) => `${a.zone} ${a.rule}`)
        .join(' ')
        .toLowerCase();
      return (
        callsign.includes(q) ||
        icao.includes(q) ||
        model.includes(q) ||
        aircraftType.includes(q) ||
        activeAlertText.includes(q)
      );
    });
    return sortFlightsBy(filtered, flightSortField, flightSortDirection, alertCountByFlight);
  }, [portal.flightsData, searchQuery, flightSortField, flightSortDirection, alertCountByFlight]);

  const paths = useFlightPaths(portalView, selection.activeFlightId, filteredFlights);

  fetchAndSetPathRef.current = paths.fetchAndSetPath;
  clearPathsIfNeededRef.current = paths.clearPathsIfNeeded;
  resetPathsRef.current = paths.resetPaths;
  setPathCoordsRef.current = paths.setPathCoords;

  useEffect(() => {
    showAllPathsRef.current = paths.showAllPaths;
  }, [paths.showAllPaths]);

  const filteredAlerts = useMemo(() => {
    const q = searchQuery.toLowerCase();
    return portal.alertsData.filter((alert) => {
      const callsign = (alert.callsign || '').toLowerCase();
      const icao = (alert.icao || '').toLowerCase();
      const zone = (alert.zone || '').toLowerCase();
      const rule = (alert.rule || '').toLowerCase();
      return (
        callsign.includes(q) ||
        icao.includes(q) ||
        zone.includes(q) ||
        rule.includes(q)
      );
    });
  }, [portal.alertsData, searchQuery]);

  const flightCount = useMemo(() => {
    if (portalView === 'live') {
      return portal.flightsData.filter((f) => f.is_live).length;
    }
    return portal.flightsData.length;
  }, [portal.flightsData, portalView]);

  const { activeFlightId, closeDrawer, setFollowSelectedPlane } = selection;

  useEffect(() => {
    if (portalView === 'live' && activeFlightId) {
      const exists = portal.flightsData.some((f) => f.flight_id === activeFlightId);
      if (!exists) {
        closeDrawer();
      }
    }
  }, [portal.flightsData, activeFlightId, portalView, closeDrawer]);

  const disableFollow = useCallback(
    () => setFollowSelectedPlane(false),
    [setFollowSelectedPlane],
  );

  return {
    portalView,
    searchQuery,
    setSearchQuery,
    zonesVisible,
    setZonesVisible,
    notificationsEnabled,
    enableNotifications,
    mapRef,
    alertNotifications,
    selection,
    portal,
    paths,
    filteredFlights,
    filteredAlerts,
    flightCount,
    flightSortField,
    flightSortDirection,
    setFlightSort,
    toggleFlightSortDirection,
    disableFollow,
  };
}

export type { DrawerTab, SidebarTab };
