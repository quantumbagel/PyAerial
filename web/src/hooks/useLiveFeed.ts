import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../api/client';
import { describeError } from '../api/errors';
import { connectLiveSocket } from '../api/liveSocket';
import type { Alert, FlightSummary, ServerStats, TelemetryPoint } from '../api/types';
import {
  alertEpisodeIdentity,
  dedupeAlerts,
  isAlertActive,
  mergeAlertsByEpisode,
} from '../utils/alertData';
import { applyTelemetryBatch, mergeLiveFlights } from '../utils/flightData';
import { useLatestRef } from './useStableCallback';

export interface LiveFeedCallbacks {
  /** Each live `flights` snapshot (after merging). */
  onFlights?: (flights: FlightSummary[]) => void;
  /** Each live telemetry batch. */
  onTelemetry?: (points: TelemetryPoint[]) => void;
  /** Number of episodes that became active since the previous alerts snapshot. */
  onAlertsActivated?: (count: number) => void;
  /** The socket (re)connected while the live view is shown. */
  onReconnect?: () => void;
}

/** Count episodes that are active in `next` but were absent or inactive in `prev`. */
export function countActivations(prev: Alert[], next: Alert[]): number {
  const before = new Map(prev.map((a) => [alertEpisodeIdentity(a), a]));
  let count = 0;
  for (const alert of next) {
    if (!isAlertActive(alert)) continue;
    const old = before.get(alertEpisodeIdentity(alert));
    if (!old || !isAlertActive(old)) count += 1;
  }
  return count;
}

function pruneUntracked(alerts: Alert[], trackedIds: Set<string>): Alert[] {
  if (!alerts.some((a) => a.flight_id && !trackedIds.has(a.flight_id))) return alerts;
  return alerts.filter((a) => !a.flight_id || trackedIds.has(a.flight_id));
}

/**
 * Live flights/alerts/stats. Data arrives as server pushes over /ws/live and is
 * re-fetched in full on every (re)connect, so there is no polling. All state
 * is dropped while `enabled` is false.
 */
export function useLiveFeed(enabled: boolean, callbacks: LiveFeedCallbacks = {}) {
  const [flights, setFlights] = useState<FlightSummary[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [stats, setStats] = useState<ServerStats | null>(null);
  const [isLoadingFlights, setIsLoadingFlights] = useState(true);
  const [isLoadingAlerts, setIsLoadingAlerts] = useState(true);
  const [flightsError, setFlightsError] = useState<string | null>(null);
  const [alertsError, setAlertsError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);
  const callbacksRef = useLatestRef(callbacks);
  // Mirrors of state so socket handlers can compute without impure updaters.
  const flightsRef = useRef<FlightSummary[]>([]);
  const alertsRef = useRef<Alert[]>([]);

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    let version = 0;
    let initialAlerts = true;

    const commitFlights = (next: FlightSummary[]) => {
      flightsRef.current = next;
      setFlights(next);
    };
    const commitAlerts = (next: Alert[]) => {
      alertsRef.current = next;
      setAlerts(next);
    };

    const fetchAll = async () => {
      const mine = ++version;
      const current = () => !cancelled && mine === version;
      const [flightsRes, alertsRes, statsRes] = await Promise.allSettled([
        api.fetchFlights('live'),
        api.fetchAlerts('live', { activeOnly: false }),
        api.fetchStats(),
      ]);
      if (!current()) return;
      if (flightsRes.status === 'fulfilled') {
        const merged = mergeLiveFlights(flightsRef.current, flightsRes.value);
        commitFlights(merged);
        setFlightsError(null);
        callbacksRef.current.onFlights?.(merged);
      } else {
        console.error('Failed to load live flights.', flightsRes.reason);
        setFlightsError(describeError(flightsRes.reason, 'Failed to load live flights.'));
      }
      if (alertsRes.status === 'fulfilled') {
        let next = mergeAlertsByEpisode(alertsRef.current, dedupeAlerts(alertsRes.value));
        if (flightsRes.status === 'fulfilled') {
          next = pruneUntracked(next, new Set(flightsRes.value.map((f) => f.flight_id)));
        }
        commitAlerts(next);
        setAlertsError(null);
      } else {
        console.error('Failed to load live alerts.', alertsRes.reason);
        setAlertsError(describeError(alertsRes.reason, 'Failed to load live alerts.'));
      }
      if (statsRes.status === 'fulfilled' && statsRes.value) setStats(statsRes.value);
      setIsLoadingFlights(false);
      setIsLoadingAlerts(false);
    };

    const unsubscribe = connectLiveSocket({
      onOpen: () => {
        initialAlerts = true;
        void fetchAll();
        callbacksRef.current.onReconnect?.();
      },
      onMessage: (message) => {
        if (cancelled) return;
        switch (message.type) {
          case 'stats':
            setStats(message.stats);
            break;
          case 'flights': {
            const merged = mergeLiveFlights(flightsRef.current, message.flights);
            commitFlights(merged);
            setIsLoadingFlights(false);
            setFlightsError(null);
            // The live store drops alerts once their flight is no longer tracked.
            const pruned = pruneUntracked(
              alertsRef.current,
              new Set(message.flights.map((f) => f.flight_id)),
            );
            if (pruned !== alertsRef.current) commitAlerts(pruned);
            callbacksRef.current.onFlights?.(merged);
            break;
          }
          case 'alerts': {
            const incoming = dedupeAlerts(message.alerts);
            const prev = alertsRef.current;
            // Merge rather than replace: the snapshot only covers tracked
            // flights, so replacing would drop their ended episodes.
            const merged = mergeAlertsByEpisode(prev, incoming);
            if (!initialAlerts) {
              const activated = countActivations(prev, merged);
              if (activated > 0) callbacksRef.current.onAlertsActivated?.(activated);
            }
            initialAlerts = false;
            commitAlerts(merged);
            setIsLoadingAlerts(false);
            setAlertsError(null);
            break;
          }
          case 'telemetry': {
            const points = message.telemetry;
            if (!points.length) break;
            const next = applyTelemetryBatch(flightsRef.current, points);
            if (next !== flightsRef.current) commitFlights(next);
            callbacksRef.current.onTelemetry?.(points);
            break;
          }
          default:
            break;
        }
      },
    });

    return () => {
      cancelled = true;
      unsubscribe();
      flightsRef.current = [];
      alertsRef.current = [];
      setFlights([]);
      setAlerts([]);
      setStats(null);
      setIsLoadingFlights(true);
      setIsLoadingAlerts(true);
      setFlightsError(null);
      setAlertsError(null);
    };
  }, [enabled, retryToken, callbacksRef]);

  /** Re-run the full fetch (re-subscribes; the socket itself stays up). */
  const retry = useCallback(() => setRetryToken((t) => t + 1), []);

  return {
    flights,
    alerts,
    stats,
    isLoadingFlights,
    isLoadingAlerts,
    flightsError,
    alertsError,
    retry,
  };
}
