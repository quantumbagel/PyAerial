import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import * as api from '../api/client';
import type { Alert, FlightSummary, PortalView, TelemetryPoint } from '../api/types';
import { mergeAlertsByEpisode } from '../utils/alertData';
import {
  OVERLAY_POINT_CAP,
  SELECTED_POINT_CAP,
  appendCapped,
  insertByTimestamp,
  trimToCap,
} from '../utils/buffers';

export const MAX_PATH_FETCHES = 4;

type LatLng = [number, number];

/** True when this id still needs a path fetch (not pending, failed, or already present). */
export function pathNeedsFetch(
  flightId: string,
  pathCoords: Record<string, LatLng[]>,
  pending: Set<string>,
  failed: Set<string>,
): boolean {
  if (pending.has(flightId) || failed.has(flightId)) {
    return false;
  }
  // Empty [] is a successful fetch with no lat/lon and must stay terminal.
  // A missing key (never fetched, or pruned) needs a fetch.
  return !Object.prototype.hasOwnProperty.call(pathCoords, flightId);
}

function hasFix(p: { latitude?: number | null; longitude?: number | null }): boolean {
  return (
    typeof p.latitude === 'number' &&
    typeof p.longitude === 'number' &&
    Number.isFinite(p.latitude) &&
    Number.isFinite(p.longitude)
  );
}

function sameCoord(a: LatLng | undefined, b: LatLng): boolean {
  return !!a && a[0] === b[0] && a[1] === b[1];
}

function hasUnwanted(paths: Record<string, unknown>, keep: Set<string>): boolean {
  for (const id in paths) if (!keep.has(id)) return true;
  return false;
}

function pruneToKeep<T>(prev: Record<string, T>, keep: Set<string>): Record<string, T> {
  let changed = false;
  const next: Record<string, T> = {};
  for (const [id, value] of Object.entries(prev)) {
    if (keep.has(id)) next[id] = value;
    else changed = true;
  }
  return changed ? next : prev;
}

/**
 * Map path data for the selected flight (or every visible flight when the
 * "Paths" toggle is on): coordinates, the telemetry behind them (for alert
 * overlays), and each flight's alert episodes. Live updates are appended with
 * bounded buffers.
 */
export function useFlightPaths(
  portalView: PortalView,
  activeFlightId: string | null,
  filteredFlights: FlightSummary[],
) {
  const [showAllPaths, setShowAllPaths] = useState(false);
  const [pathCoords, setPathCoords] = useState<Record<string, LatLng[]>>({});
  const [pathTelemetry, setPathTelemetry] = useState<Record<string, TelemetryPoint[]>>({});
  const [pathAlerts, setPathAlerts] = useState<Record<string, Alert[]>>({});
  // Bumped when a fetch fails so the queue can start the next id without
  // waiting for pathCoords to change.
  const [fetchWave, setFetchWave] = useState(0);

  const pendingPathFetches = useRef(new Set<string>());
  const failedPathFetches = useRef(new Set<string>());
  /** Overlay paths that missed live points (paused tab, reconnect): refetched in place. */
  const stalePaths = useRef(new Set<string>());
  // Latest view/selection for in-flight fetches: after await we must discard
  // results that no longer belong, not apply the values from the starting render.
  const wantRef = useRef({ portalView, showAllPaths, activeFlightId });
  useLayoutEffect(() => {
    wantRef.current = { portalView, showAllPaths, activeFlightId };
  });

  const isTracked = useCallback(
    (flightId: string) => showAllPaths || flightId === activeFlightId,
    [showAllPaths, activeFlightId],
  );
  const capFor = useCallback(
    (flightId: string) => (flightId === activeFlightId ? SELECTED_POINT_CAP : OVERLAY_POINT_CAP),
    [activeFlightId],
  );

  const fetchAndSetPath = useCallback(async (flightId: string, view: PortalView, force = false) => {
    if (pendingPathFetches.current.has(flightId)) return;
    pendingPathFetches.current.add(flightId);
    // A stale path is replaced whole: the fetch covers the points it missed.
    const replace = stalePaths.current.delete(flightId) || force;
    try {
      const [telemetry, alerts] = await Promise.all([
        api.fetchTelemetry(flightId, view),
        api.fetchAlerts(view, { flightId, activeOnly: false }),
      ]);
      const want = wantRef.current;
      if (want.portalView !== view) return;
      if (!(want.showAllPaths || flightId === want.activeFlightId)) return;
      const cap = flightId === want.activeFlightId ? SELECTED_POINT_CAP : OVERLAY_POINT_CAP;
      const valid = trimToCap(
        telemetry.filter(hasFix).sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0)),
        cap,
      );
      const latlngs = valid.map((p) => [p.latitude!, p.longitude!] as LatLng);
      setPathCoords((prev) => ({ ...prev, [flightId]: latlngs }));
      setPathTelemetry((prev) => ({
        ...prev,
        // Keep live points that arrived during the fetch.
        [flightId]: replace ? valid : insertByTimestamp(valid, prev[flightId] ?? [], cap),
      }));
      setPathAlerts((prev) => ({ ...prev, [flightId]: alerts }));
    } catch (err) {
      console.error('Failed to fetch flight path', err);
      failedPathFetches.current.add(flightId);
      setFetchWave((n) => n + 1);
    } finally {
      pendingPathFetches.current.delete(flightId);
    }
  }, []);

  // Paths that may exist: the selection, plus every listed flight with "Paths" on.
  const keep = useMemo(() => {
    const ids = new Set(showAllPaths ? filteredFlights.map((f) => f.flight_id) : []);
    if (activeFlightId) ids.add(activeFlightId);
    return ids;
  }, [showAllPaths, filteredFlights, activeFlightId]);
  // Drop the others while rendering (not in an effect after commit), so a path
  // that no longer belongs is never drawn. Converges: pruning leaves none behind.
  if (hasUnwanted(pathCoords, keep) || hasUnwanted(pathTelemetry, keep) || hasUnwanted(pathAlerts, keep)) {
    setPathCoords((prev) => pruneToKeep(prev, keep));
    setPathTelemetry((prev) => pruneToKeep(prev, keep));
    setPathAlerts((prev) => pruneToKeep(prev, keep));
  }

  const clearAll = useCallback(() => {
    setPathCoords({});
    setPathTelemetry({});
    setPathAlerts({});
  }, []);

  const resetPaths = useCallback(() => {
    failedPathFetches.current.clear();
    pendingPathFetches.current.clear();
    stalePaths.current.clear();
    clearAll();
  }, [clearAll]);

  // Selection / toggle / view changes decide which paths exist.
  useEffect(() => {
    failedPathFetches.current.clear();
    stalePaths.current.clear();
    if (showAllPaths) return;
    // False positive: fetchAndSetPath is async and only sets state after its await.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (activeFlightId) void fetchAndSetPath(activeFlightId, portalView);
  }, [showAllPaths, activeFlightId, portalView, fetchAndSetPath]);

  // With "Paths" on: fetch missing paths (unlisted ones were pruned above).
  useEffect(() => {
    if (!showAllPaths) return;
    for (const id of stalePaths.current) if (!keep.has(id)) stalePaths.current.delete(id);
    const candidates = activeFlightId
      ? [activeFlightId, ...filteredFlights.map((f) => f.flight_id)]
      : filteredFlights.map((f) => f.flight_id);
    const missing = candidates.filter((id) =>
      stalePaths.current.has(id)
        ? !pendingPathFetches.current.has(id)
        : pathNeedsFetch(id, pathCoords, pendingPathFetches.current, failedPathFetches.current),
    );
    const room = Math.max(0, MAX_PATH_FETCHES - pendingPathFetches.current.size);
    missing.slice(0, room).forEach((id) => {
      void fetchAndSetPath(id, portalView);
    });
  }, [
    showAllPaths,
    filteredFlights,
    keep,
    portalView,
    fetchAndSetPath,
    activeFlightId,
    pathCoords,
    fetchWave,
  ]);

  /** Extend tracked paths from a live flights snapshot. */
  const appendFlightPositions = useCallback(
    (flights: FlightSummary[]) => {
      setPathCoords((prev) => {
        let next = prev;
        for (const f of flights) {
          if (!f.flight_id || !hasFix(f) || !isTracked(f.flight_id)) continue;
          const existing = next[f.flight_id];
          // Paths start from a fetch; don't seed a path from a single fix.
          if (!existing) continue;
          const coord: LatLng = [f.latitude!, f.longitude!];
          if (sameCoord(existing[existing.length - 1], coord)) continue;
          if (next === prev) next = { ...prev };
          next[f.flight_id] = appendCapped(existing, coord, capFor(f.flight_id));
        }
        return next;
      });
    },
    [capFor, isTracked],
  );

  /** Extend tracked paths (and their telemetry) from a live telemetry batch. */
  const appendLiveTelemetry = useCallback(
    (points: TelemetryPoint[]) => {
      const tracked = points.filter(
        (p): p is TelemetryPoint & { flight_id: string } =>
          !!p.flight_id && hasFix(p) && isTracked(p.flight_id),
      );
      if (!tracked.length) return;
      setPathCoords((prev) => {
        let next = prev;
        for (const p of tracked) {
          const existing = next[p.flight_id];
          if (!existing) continue;
          const coord: LatLng = [p.latitude!, p.longitude!];
          if (sameCoord(existing[existing.length - 1], coord)) continue;
          if (next === prev) next = { ...prev };
          next[p.flight_id] = appendCapped(existing, coord, capFor(p.flight_id));
        }
        return next;
      });
      setPathTelemetry((prev) => {
        let next = prev;
        const byFlight = new Map<string, TelemetryPoint[]>();
        for (const p of tracked) {
          const list = byFlight.get(p.flight_id);
          if (list) list.push(p);
          else byFlight.set(p.flight_id, [p]);
        }
        for (const [flightId, pts] of byFlight) {
          const existing = next[flightId];
          if (!existing) continue;
          const merged = insertByTimestamp(existing, pts, capFor(flightId));
          if (merged === existing) continue;
          if (next === prev) next = { ...prev };
          next[flightId] = merged;
        }
        return next;
      });
    },
    [capFor, isTracked],
  );

  /** Fold live alert snapshots into the alert overlays of flights with a path. */
  const mergeLiveAlerts = useCallback((alerts: Alert[]) => {
    const byFlight = new Map<string, Alert[]>();
    for (const alert of alerts) {
      if (!alert.flight_id) continue;
      const arr = byFlight.get(alert.flight_id);
      if (arr) arr.push(alert);
      else byFlight.set(alert.flight_id, [alert]);
    }
    if (byFlight.size === 0) return;
    setPathAlerts((prev) => {
      let next = prev;
      for (const [flightId, list] of byFlight) {
        const existing = prev[flightId];
        if (!existing || existing.length === 0) continue;
        const merged = mergeAlertsByEpisode(existing, list);
        if (merged.length !== existing.length || merged.some((a, i) => a !== existing[i])) {
          if (next === prev) next = { ...prev };
          next[flightId] = merged;
        }
      }
      return next;
    });
  }, []);

  /** Re-read the selected flight's path (after a reconnect). */
  const refetchActivePath = useCallback(() => {
    if (activeFlightId) void fetchAndSetPath(activeFlightId, portalView, true);
  }, [activeFlightId, fetchAndSetPath, portalView]);

  /**
   * After live points were missed (paused tab, reconnect): re-read the
   * selected path now and every overlay in turn, keeping them on screen
   * meanwhile, so no path is left joining across the gap.
   */
  const resyncPaths = useCallback(() => {
    refetchActivePath();
    if (!showAllPaths) return;
    failedPathFetches.current.clear();
    for (const id of Object.keys(pathCoords)) {
      if (id !== activeFlightId) stalePaths.current.add(id);
    }
    setFetchWave((n) => n + 1);
  }, [refetchActivePath, showAllPaths, pathCoords, activeFlightId]);

  return {
    showAllPaths,
    setShowAllPaths,
    pathCoords,
    pathTelemetry,
    pathAlerts,
    resetPaths,
    appendFlightPositions,
    appendLiveTelemetry,
    mergeLiveAlerts,
    refetchActivePath,
    resyncPaths,
  };
}
