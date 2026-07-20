import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Alert, PortalView } from '../api/types';
import type { DrawerTab } from '../components/DetailsDrawer';
import type { MapViewHandle } from '../components/MapView';
import type { SidebarTab } from '../components/Sidebar';
import { useAlertNotifications } from './useAlertNotifications';
import { useFlightPaths } from './useFlightPaths';
import { useFlightSelection } from './useFlightSelection';
import { usePortalData } from './usePortalData';

export function usePortalApp() {
  const [portalView, setPortalView] = useState<PortalView>(() => {
    const saved = localStorage.getItem('portalView');
    return (saved === 'live' || saved === 'history') ? saved : 'live';
  });
  const [searchQuery, setSearchQuery] = useState('');
  const [zonesVisible, setZonesVisible] = useState(true);

  useEffect(() => {
    localStorage.setItem('portalView', portalView);
  }, [portalView]);

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

  const onNewAlerts = useCallback(
    (newAlerts: Alert[]) => {
      const newest = newAlerts[0];
      alertNotifications.playWarningChime(newest.level || 'warn');
      alertNotifications.triggerDesktopNotification(newest);
      newAlerts.forEach((a) => alertNotifications.addToast(a));
    },
    [alertNotifications],
  );

  const selection = useFlightSelection({
    portalView,
    mapRef,
    fetchAndSetPathRef,
    clearPathsIfNeeded: () => clearPathsIfNeededRef.current(),
    onSelectAlertTab: () => onSelectAlertTabRef.current(),
  });

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

  const filteredFlights = useMemo(() => {
    const q = searchQuery.toLowerCase();
    return portal.flightsData.filter((flight) => {
      const callsign = (flight.callsign || '').toLowerCase();
      const icao = (flight.icao || '').toLowerCase();
      const model = (flight.model || '').toLowerCase();
      const aircraftType = (flight.aircraft_type || flight.typecode || '').toLowerCase();
      const zone = (flight.zone || '').toLowerCase();
      return (
        callsign.includes(q) ||
        icao.includes(q) ||
        model.includes(q) ||
        aircraftType.includes(q) ||
        zone.includes(q)
      );
    });
  }, [portal.flightsData, searchQuery]);

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
      const level = (alert.level || '').toLowerCase();
      return (
        callsign.includes(q) ||
        icao.includes(q) ||
        zone.includes(q) ||
        level.includes(q)
      );
    });
  }, [portal.alertsData, searchQuery]);

  const flightCount = useMemo(() => {
    if (portalView === 'live') {
      return portal.flightsData.filter((f) => f.is_live).length;
    }
    return portal.flightsData.length;
  }, [portal.flightsData, portalView]);

  useEffect(() => {
    if (portalView === 'live' && selection.activeFlightId) {
      const exists = portal.flightsData.some((f) => f.flight_id === selection.activeFlightId);
      if (!exists) {
        selection.closeDrawer();
      }
    }
  }, [portal.flightsData, selection.activeFlightId, portalView, selection.closeDrawer]);

  const disableFollow = useCallback(
    () => selection.setFollowSelectedPlane(false),
    [selection.setFollowSelectedPlane],
  );

  return {
    portalView,
    searchQuery,
    setSearchQuery,
    zonesVisible,
    setZonesVisible,
    mapRef,
    alertNotifications,
    selection,
    portal,
    paths,
    filteredFlights,
    filteredAlerts,
    flightCount,
    disableFollow,
  };
}

export type { DrawerTab, SidebarTab };
