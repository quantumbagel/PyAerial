import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../api/client';
import type { Alert, FlightSummary, PortalView, TelemetryPoint } from '../api/types';

export function useFlightPaths(
  portalView: PortalView,
  activeFlightId: string | null,
  filteredFlights: FlightSummary[],
) {
  const [showAllPaths, setShowAllPaths] = useState(false);
  const [pathCoords, setPathCoords] = useState<Record<string, [number, number][]>>({});
  const [pathTelemetry, setPathTelemetry] = useState<Record<string, TelemetryPoint[]>>({});
  const [pathAlerts, setPathAlerts] = useState<Record<string, Alert[]>>({});

  const pendingPathFetches = useRef(new Set<string>());
  const showAllPathsRef = useRef(showAllPaths);
  const pathCoordsRef = useRef(pathCoords);
  const filteredFlightsRef = useRef(filteredFlights);
  const portalViewRef = useRef(portalView);
  const activeFlightIdRef = useRef(activeFlightId);

  useEffect(() => {
    showAllPathsRef.current = showAllPaths;
  }, [showAllPaths]);
  useEffect(() => {
    pathCoordsRef.current = pathCoords;
  }, [pathCoords]);
  useEffect(() => {
    filteredFlightsRef.current = filteredFlights;
  }, [filteredFlights]);
  useEffect(() => {
    portalViewRef.current = portalView;
  }, [portalView]);
  useEffect(() => {
    activeFlightIdRef.current = activeFlightId;
  }, [activeFlightId]);

  const fetchAndSetPath = useCallback(async (flightId: string, view: PortalView) => {
    if (pendingPathFetches.current.has(flightId)) return;
    pendingPathFetches.current.add(flightId);
    try {
      const [telemetry, alerts] = await Promise.all([
        api.fetchTelemetry(flightId, view),
        api.fetchAlerts(view, { flightId, activeOnly: false }),
      ]);
      if (portalViewRef.current !== view) return;
      if (!showAllPathsRef.current && activeFlightIdRef.current !== flightId) return;
      const validTelemetry = telemetry.filter((p) => p.latitude != null && p.longitude != null);
      const latlngs = validTelemetry.map((p) => [p.latitude!, p.longitude!] as [number, number]);
      if (latlngs.length === 0) {
        setPathCoords((prev) => {
          const next = { ...prev };
          delete next[flightId];
          return next;
        });
        setPathTelemetry((prev) => {
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
        setPathTelemetry((prev) => ({ ...prev, [flightId]: validTelemetry }));
        setPathAlerts((prev) => ({ ...prev, [flightId]: alerts }));
      }
    } catch (err) {
      console.error('Failed to fetch flight path', err);
    } finally {
      pendingPathFetches.current.delete(flightId);
    }
  }, []);

  const refreshFlightPaths = useCallback(
    async (flightId: string | null, view: PortalView) => {
      if (showAllPathsRef.current) {
        const missing = filteredFlightsRef.current.filter(
          (f) => !pathCoordsRef.current[f.flight_id],
        );
        await Promise.all(missing.map((f) => fetchAndSetPath(f.flight_id, view)));
      } else if (flightId) {
        setPathCoords({});
        setPathTelemetry({});
        setPathAlerts({});
        await fetchAndSetPath(flightId, view);
      } else {
        setPathCoords({});
        setPathTelemetry({});
        setPathAlerts({});
      }
    },
    [fetchAndSetPath],
  );

  const clearPathsIfNeeded = useCallback(() => {
    if (!showAllPaths) {
      setPathCoords({});
      setPathTelemetry({});
      setPathAlerts({});
    }
  }, [showAllPaths]);

  const resetPaths = useCallback(() => {
    setPathCoords({});
    setPathTelemetry({});
    setPathAlerts({});
  }, []);

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

  return {
    showAllPaths,
    setShowAllPaths,
    pathCoords,
    setPathCoords,
    pathTelemetry,
    setPathTelemetry,
    pathAlerts,
    setPathAlerts,
    fetchAndSetPath,
    clearPathsIfNeeded,
    resetPaths,
  };
}
