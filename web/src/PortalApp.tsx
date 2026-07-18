import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as api from './api/client';
import { connectLiveSocket } from './api/liveSocket';
import type { Alert, FlightDetail, FlightSummary, PortalView, TelemetryPoint, ZonesData } from './api/types';
import { DetailsDrawer, type DrawerTab } from './components/DetailsDrawer';
import { MapView, type MapViewHandle } from './components/MapView';
import { Sidebar, type SidebarTab, type WarningFilter } from './components/Sidebar';
import { isFlightLive } from './utils/format';
import { COLOR_CONFIG } from './utils/colors';

const ALERTS_LIMIT = 50;

function mergeLiveFlights(existing: FlightSummary[], incoming: FlightSummary[]): FlightSummary[] {
  const next = [...existing];
  incoming.forEach((newFlight) => {
    const idx = next.findIndex((f) => f.flight_id === newFlight.flight_id);
    if (idx !== -1) {
      if (isFlightLive(next[idx])) {
        next[idx] = {
          ...next[idx],
          callsign: newFlight.callsign || next[idx].callsign,
          model: newFlight.model || next[idx].model,
          aircraft_type: newFlight.aircraft_type || next[idx].aircraft_type,
          owner: newFlight.owner || next[idx].owner,
          country: newFlight.country || next[idx].country,
          zone: newFlight.zone,
          level: newFlight.level,
          latitude: newFlight.latitude ?? next[idx].latitude,
          longitude: newFlight.longitude ?? next[idx].longitude,
          altitude: newFlight.altitude ?? next[idx].altitude,
          speed: newFlight.speed ?? next[idx].speed,
          heading: newFlight.heading ?? next[idx].heading,
          timestamp: newFlight.timestamp ?? next[idx].timestamp,
          end_time: newFlight.end_time ?? next[idx].end_time,
        };
      } else {
        next[idx] = newFlight;
      }
    } else {
      next.push(newFlight);
    }
  });
  const remoteIds = new Set(incoming.map((f) => f.flight_id));
  return next.filter(
    (f) => remoteIds.has(f.flight_id),
  );
}

function applyTelemetryPoint(
  flights: FlightSummary[],
  point: TelemetryPoint,
): FlightSummary[] {
  const flightId = point.flight_id;
  if (!flightId || point.latitude == null || point.longitude == null) return flights;
  const next = [...flights];
  let flight = next.find((f) => f.flight_id === flightId);
  if (!flight) {
    flight = {
      flight_id: flightId,
      icao: point.icao || '',
      callsign: null,
      model: null,
      aircraft_type: null,
      owner: null,
      country: null,
      zone: point.zone || '',
      level: point.level || '',
      start_time: point.timestamp,
      end_time: point.timestamp,
      timestamp: point.timestamp,
      latitude: point.latitude,
      longitude: point.longitude,
      heading: point.heading,
      altitude: point.altitude,
      speed: point.speed,
      is_live: true,
    };
    next.push(flight);
  } else {
    const idx = next.indexOf(flight);
    next[idx] = {
      ...flight,
      latitude: point.latitude,
      longitude: point.longitude,
      altitude: point.altitude,
      speed: point.speed,
      heading: point.heading,
      timestamp: point.timestamp,
      end_time: point.timestamp,
      zone: point.zone || flight.zone,
      level: point.level || flight.level,
      is_live: true,
    };
  }
  return next;
}

function sortFlights(flights: FlightSummary[]): FlightSummary[] {
  return [...flights].sort((a, b) => {
    const aLive = isFlightLive(a);
    const bLive = isFlightLive(b);
    if (aLive && !bLive) return -1;
    if (!aLive && bLive) return 1;
    return (b.start_time || 0) - (a.start_time || 0);
  });
}

export function PortalApp() {
  const [portalView, setPortalView] = useState<PortalView>('live');
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>('flights');
  const [searchQuery, setSearchQuery] = useState('');
  const [warningFilter, setWarningFilter] = useState<WarningFilter>('all');
  const [flightsData, setFlightsData] = useState<FlightSummary[]>([]);
  const [alertsData, setAlertsData] = useState<Alert[]>([]);
  const [activeFlightId, setActiveFlightId] = useState<string | null>(null);
  const [activeAlertId, setActiveAlertId] = useState<string | null>(null);
  const [flightDetail, setFlightDetail] = useState<FlightDetail | null>(null);
  const [flightAlerts, setFlightAlerts] = useState<Alert[]>([]);
  const [flightTelemetry, setFlightTelemetry] = useState<TelemetryPoint[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState<DrawerTab>('alerts');
  const [followSelectedPlane, setFollowSelectedPlane] = useState(false);
  const [zonesVisible, setZonesVisible] = useState(true);
  const [showAllPaths, setShowAllPaths] = useState(false);
  const [zonesData, setZonesData] = useState<ZonesData | null>(null);
  const [pathCoords, setPathCoords] = useState<Record<string, [number, number][]>>({});
  const [pathAlerts, setPathAlerts] = useState<Record<string, Alert[]>>({});
  const [selectedTelemetryPoint, setSelectedTelemetryPoint] = useState<TelemetryPoint | null>(null);

  const mapRef = useRef<MapViewHandle>({
    map: null,
    fitPathBounds: () => {},
    panToAlert: () => {},
  });
  const pendingPathFetches = useRef(new Set<string>());
  const hasMoreAlerts = useRef(true);
  const isFetchingAlerts = useRef(false);
  const flightDetailsPollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const activeFlightIdRef = useRef<string | null>(null);
  const portalViewRef = useRef<PortalView>('live');
  const showAllPathsRef = useRef(showAllPaths);
  const flightAlertsRef = useRef(flightAlerts);
  const pathCoordsRef = useRef(pathCoords);

  useEffect(() => {
    Object.entries(COLOR_CONFIG).forEach(([key, val]) => {
      const cssKey = `--${key.replace(/([A-Z])/g, '-$1').toLowerCase()}`;
      document.documentElement.style.setProperty(cssKey, val);
    });
  }, []);

  useEffect(() => {
    activeFlightIdRef.current = activeFlightId;
  }, [activeFlightId]);
  useEffect(() => {
    portalViewRef.current = portalView;
  }, [portalView]);
  useEffect(() => {
    showAllPathsRef.current = showAllPaths;
  }, [showAllPaths]);
  useEffect(() => {
    flightAlertsRef.current = flightAlerts;
  }, [flightAlerts]);
  useEffect(() => {
    pathCoordsRef.current = pathCoords;
  }, [pathCoords]);

  const filteredFlights = useMemo(() => {
    const q = searchQuery.toLowerCase();
    return flightsData.filter((flight) => {
      const callsign = (flight.callsign || '').toLowerCase();
      const icao = (flight.icao || '').toLowerCase();
      const model = (flight.model || '').toLowerCase();
      const aircraftType = (flight.aircraft_type || flight.typecode || '').toLowerCase();
      const matchesSearch =
        callsign.includes(q) || icao.includes(q) || model.includes(q) || aircraftType.includes(q);
      const level = (flight.level || '').toLowerCase();
      let matchesWarning = true;
      if (warningFilter === 'warn') matchesWarning = level === 'warn';
      else if (warningFilter === 'alert') matchesWarning = level === 'alert';
      else if (warningFilter === 'any') matchesWarning = level === 'warn' || level === 'alert';
      return matchesSearch && matchesWarning;
    });
  }, [flightsData, searchQuery, warningFilter]);

  const filteredFlightsRef = useRef(filteredFlights);
  useEffect(() => {
    filteredFlightsRef.current = filteredFlights;
  }, [filteredFlights]);



  const flightCount = useMemo(() => {
    if (portalView === 'live') {
      return flightsData.filter((f) => f.is_live).length;
    }
    return flightsData.length;
  }, [flightsData, portalView]);

  const loadZones = useCallback(async () => {
    try {
      const data = await api.fetchZones();
      setZonesData(data);
    } catch (err) {
      console.error('Failed to fetch zones', err);
    }
  }, []);

  const fetchAndSetPath = useCallback(
    async (flightId: string, view: PortalView) => {
      if (pendingPathFetches.current.has(flightId)) return;
      pendingPathFetches.current.add(flightId);
      try {
        const [telemetry, alerts] = await Promise.all([
          api.fetchTelemetry(flightId, view),
          api.fetchAlerts(view, { flightId }),
        ]);
        const latlngs = telemetry
          .filter((p) => p.latitude != null && p.longitude != null)
          .map((p) => [p.latitude!, p.longitude!] as [number, number]);
        if (latlngs.length === 0) {
          setPathCoords((prev) => {
            const next = { ...prev };
            delete next[flightId];
            return next;
          });
          setPathAlerts((prev) => {
            const next = { ...prev };
            delete next[flightId];
            return next;
          });
        } else {
          setPathCoords((prev) => ({ ...prev, [flightId]: latlngs }));
          setPathAlerts((prev) => ({ ...prev, [flightId]: alerts }));
        }
      } catch (err) {
        console.error('Failed to fetch flight path', flightId, err);
      } finally {
        pendingPathFetches.current.delete(flightId);
      }
    },
    [],
  );

  const refreshFlightPaths = useCallback(
    async (flightId: string | null, view: PortalView) => {
      if (showAllPathsRef.current) {
        const missing = filteredFlightsRef.current.filter(
          (f) => !pathCoordsRef.current[f.flight_id],
        );
        await Promise.all(missing.map((f) => fetchAndSetPath(f.flight_id, view)));
      } else if (flightId) {
        setPathCoords({});
        setPathAlerts({});
        await fetchAndSetPath(flightId, view);
      } else {
        setPathCoords({});
        setPathAlerts({});
      }
    },
    [fetchAndSetPath],
  );

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

  const selectFlight = useCallback(
    async (flightId: string) => {
      if (flightDetailsPollTimer.current) {
        clearInterval(flightDetailsPollTimer.current);
        flightDetailsPollTimer.current = null;
      }
      setActiveFlightId(flightId);
      setSelectedTelemetryPoint(null);
      setFollowSelectedPlane(true);
      setDrawerOpen(true);
      setDrawerTab('alerts');
      try {
        const detail = await api.fetchFlight(flightId, portalView);
        setFlightDetail(detail);
        await loadFlightTelemetry(flightId, portalView);
        await loadFlightAlerts(flightId, portalView);
        await fetchAndSetPath(flightId, portalView);
        if (detail.latitude != null && detail.longitude != null && mapRef.current.map) {
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
    [portalView, loadFlightTelemetry, loadFlightAlerts, fetchAndSetPath],
  );

  const selectAlert = useCallback(
    async (alert: Alert) => {
      setActiveAlertId(alert.alert_id);
      setSidebarTab('alerts');
      if (alert.latitude != null && alert.longitude != null) {
        mapRef.current.panToAlert(alert.latitude, alert.longitude);
      }
      if (alert.flight_id) {
        await selectFlight(alert.flight_id);
        setDrawerTab('alerts');
      }
    },
    [selectFlight],
  );

  const closeDrawer = useCallback(() => {
    if (flightDetailsPollTimer.current) {
      clearInterval(flightDetailsPollTimer.current);
      flightDetailsPollTimer.current = null;
    }
    setDrawerOpen(false);
    setActiveFlightId(null);
    setSelectedTelemetryPoint(null);
    setFollowSelectedPlane(false);
    setFlightDetail(null);
    setFlightAlerts([]);
    setFlightTelemetry([]);
    if (!showAllPaths) {
      setPathCoords({});
      setPathAlerts({});
    }
  }, [showAllPaths]);

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
      if (flightDetailsPollTimer.current) {
        clearInterval(flightDetailsPollTimer.current);
        flightDetailsPollTimer.current = null;
      }
      setPortalView(view);
      setActiveFlightId(null);
      setActiveAlertId(null);
      setFollowSelectedPlane(false);
      setFlightsData([]);
      setAlertsData([]);
      setPathCoords({});
      setPathAlerts({});
      setDrawerOpen(false);
      setFlightDetail(null);
      setFlightAlerts([]);
      setFlightTelemetry([]);
    },
    [portalView],
  );

  useEffect(() => {
    loadZones();
  }, [loadZones]);

  useEffect(() => {
    if (portalView !== 'live') return undefined;
    const interval = setInterval(() => {
      const cutoff = Date.now() / 1000 - 30; // 30 seconds inactivity
      setFlightsData((prev) => {
        const next = prev.filter((f) => !f.is_live || (f.timestamp && f.timestamp >= cutoff));
        if (next.length !== prev.length) {
          const activeId = activeFlightIdRef.current;
          if (activeId && !next.some((f) => f.flight_id === activeId)) {
            closeDrawer();
          }
          return next;
        }
        return prev;
      });
    }, 5000);
    return () => clearInterval(interval);
  }, [portalView, closeDrawer]);

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
          setFlightsData((prev) => sortFlights(mergeLiveFlights(prev, message.flights)));
        } else if (message.type === 'alerts') {
          setAlertsData(message.alerts);
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
              setFlightTelemetry((prev) => {
                const ts = new Set(prev.map((t) => t.timestamp));
                const merged = [
                  ...prev,
                  ...selectedPoints.filter((p) => !ts.has(p.timestamp)),
                ].sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
                return merged;
              });
              setPathCoords((prev) => {
                const existing = prev[flightId] || [];
                const added = selectedPoints
                  .filter((p) => p.latitude != null && p.longitude != null)
                  .map((p) => [p.latitude!, p.longitude!] as [number, number]);
                return { ...prev, [flightId]: [...existing, ...added] };
              });
              loadFlightAlerts(flightId, 'live', true);
            }
          }
          if (showAllPathsRef.current) {
            message.telemetry.forEach((point) => {
              if (!point.flight_id) return;
              setPathCoords((prev) => {
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
  }, [portalView, loadFlightAlerts]);

  useEffect(() => {
    refreshFlightPaths(activeFlightId, portalView);
  }, [showAllPaths, activeFlightId, portalView, refreshFlightPaths]);

  useEffect(() => {
    if (!showAllPaths || !portalView) return;
    filteredFlights.forEach((f) => {
      if (
        !pathCoordsRef.current[f.flight_id] &&
        !pendingPathFetches.current.has(f.flight_id)
      ) {
        fetchAndSetPath(f.flight_id, portalView);
      }
    });
  }, [showAllPaths, filteredFlights, portalView, fetchAndSetPath]);

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

  const disableFollow = useCallback(() => setFollowSelectedPlane(false), []);

  return (
    <>
      <Sidebar
        portalView={portalView}
        sidebarTab={sidebarTab}
        searchQuery={searchQuery}
        warningFilter={warningFilter}
        flights={filteredFlights}
        alerts={alertsData}
        activeFlightId={activeFlightId}
        activeAlertId={activeAlertId}
        flightCount={flightCount}
        onSwitchPortalView={switchPortalView}
        onSwitchSidebarTab={setSidebarTab}
        onSearchChange={setSearchQuery}
        onWarningFilterChange={setWarningFilter}
        onSelectFlight={selectFlight}
        onSelectAlert={selectAlert}
        onAlertsScroll={handleAlertsScroll}
      />
      <MapView
        flights={flightsData}
        filteredFlights={filteredFlights}
        activeFlightId={activeFlightId}
        selectedTelemetryPoint={selectedTelemetryPoint}
        followSelectedPlane={followSelectedPlane}
        zonesVisible={zonesVisible}
        showAllPaths={showAllPaths}
        zonesData={zonesData}
        pathCoords={pathCoords}
        pathAlerts={pathAlerts}
        onSelectFlight={selectFlight}
        onFollowDisabled={disableFollow}
        onToggleFollow={() => {
          if (!activeFlightId) return;
          if (followSelectedPlane) {
            setFollowSelectedPlane(false);
          } else {
            setFollowSelectedPlane(true);
            const flight = flightsData.find((f) => f.flight_id === activeFlightId);
            if (flight?.latitude != null && flight?.longitude != null && mapRef.current.map) {
              mapRef.current.map.setView(
                [flight.latitude, flight.longitude],
                Math.max(mapRef.current.map.getZoom(), 11),
              );
            }
          }
        }}
        onToggleZones={() => setZonesVisible((v) => !v)}
        onTogglePaths={() => setShowAllPaths((v) => !v)}
        followLabel={followSelectedPlane ? 'Following' : 'Follow'}
        zonesLabel={zonesVisible ? 'Zones On' : 'Zones Off'}
        pathsLabel={showAllPaths ? 'Paths On' : 'Paths Off'}
        followVisible={!!activeFlightId}
        followActive={followSelectedPlane}
        zonesActive={zonesVisible}
        pathsActive={showAllPaths}
        mapRef={mapRef}
        drawer={
          <DetailsDrawer
            open={drawerOpen}
            flightDetail={flightDetail}
            activeAlertId={activeAlertId}
            flightAlerts={flightAlerts}
            flightTelemetry={flightTelemetry}
            drawerTab={drawerTab}
            selectedTelemetryPoint={selectedTelemetryPoint}
            onSelectTelemetryPoint={setSelectedTelemetryPoint}
            onClose={closeDrawer}
            onSwitchTab={setDrawerTab}
            onSelectAlert={selectAlert}
          />
        }
      />
    </>
  );
}
