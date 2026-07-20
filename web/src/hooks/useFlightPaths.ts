import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../api/client';
import type { Alert, FlightSummary, PortalView } from '../api/types';

export function useFlightPaths(
  portalView: PortalView,
  activeFlightId: string | null,
  filteredFlights: FlightSummary[],
) {
  const [showAllPaths, setShowAllPaths] = useState(false);
  const [pathCoords, setPathCoords] = useState<Record<string, [number, number][]>>({});
  const [pathAlerts, setPathAlerts] = useState<Record<string, Alert[]>>({});

  const pendingPathFetches = useRef(new Set<string>());
  const showAllPathsRef = useRef(showAllPaths);
  const pathCoordsRef = useRef(pathCoords);
  const filteredFlightsRef = useRef(filteredFlights);

  useEffect(() => {
    showAllPathsRef.current = showAllPaths;
  }, [showAllPaths]);
  useEffect(() => {
    pathCoordsRef.current = pathCoords;
  }, [pathCoords]);
  useEffect(() => {
    filteredFlightsRef.current = filteredFlights;
  }, [filteredFlights]);

  const fetchAndSetPath = useCallback(async (flightId: string, view: PortalView) => {
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
        setPathAlerts({});
        await fetchAndSetPath(flightId, view);
      } else {
        setPathCoords({});
        setPathAlerts({});
      }
    },
    [fetchAndSetPath],
  );

  const clearPathsIfNeeded = useCallback(() => {
    if (!showAllPaths) {
      setPathCoords({});
      setPathAlerts({});
    }
  }, [showAllPaths]);

  const resetPaths = useCallback(() => {
    setPathCoords({});
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
    pathAlerts,
    showAllPathsRef,
    fetchAndSetPath,
    clearPathsIfNeeded,
    resetPaths,
  };
}
