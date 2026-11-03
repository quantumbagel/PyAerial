import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import * as api from '../api/client';
import { describeError } from '../api/errors';
import { connectLiveSocket, setLiveStreams } from '../api/liveSocket';
import type { Alert, FlightSummary, ServerStats, TelemetryPoint } from '../api/types';
import { isAlertActive, mergeAlertsByEpisode } from '../utils/alertData';
import { applyTelemetryBatch, mergeLiveFlights } from '../utils/flightData';

export interface LiveFeedCallbacks {
  /** Each live `flights` snapshot (after merging). */
  onFlights?: (flights: FlightSummary[]) => void;
  /** Each live telemetry batch. */
  onTelemetry?: (points: TelemetryPoint[]) => void;
  /** Number of episodes that became active since the previous alerts snapshot. */
  onAlertsActivated?: (count: number) => void;
  /** The socket (re)connected while the live view is shown. */
  onReconnect?: () => void;
  /** The tab is visible again after its heavy streams were paused. */
  onResume?: () => void;
}

/** Streams kept while the tab is hidden: the unread badge and status stay current. */
export const HIDDEN_STREAMS = ['alerts', 'stats'];
/** While Live isn't shown (History polls its own stats): the server needs at least one stream. */
export const IDLE_STREAMS = ['stats'];
/** A tab must stay hidden this long before flights/telemetry pause (quick switches don't churn). */
export const HIDDEN_PAUSE_MS = 10_000;

/** Count episodes that are active in `next` but were absent or inactive in `prev`. */
export function countActivations(prev: Alert[], next: Alert[]): number {
  const before = new Map(prev.map((a) => [a.alert_id, a]));
  let count = 0;
  for (const alert of next) {
    if (!isAlertActive(alert)) continue;
    const old = before.get(alert.alert_id);
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
  // Socket handlers outlive the render that created them; they read this, not
  // a stale closure. Re-subscribing on every callback identity change would
  // refetch the whole live store.
  const callbacksRef = useRef(callbacks);
  useLayoutEffect(() => {
    callbacksRef.current = callbacks;
  });
  // Written in the same tick as setState so merge/prune in the socket handler
  // sees the lists the UI has, including updates that have not committed yet.
  const flightsRef = useRef<FlightSummary[]>([]);
  const alertsRef = useRef<Alert[]>([]);

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    let version = 0;
    let initialAlerts = true;
    // The server pushes after every change, so a push that arrives while a
    // fetch is in flight is at least as current as the fetch's answer, and
    // nothing would ever correct a stale answer (e.g. resurrected aircraft).
    const pushed = { flights: 0, alerts: 0, stats: 0 };

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
      const before = { ...pushed };
      const [flightsRes, alertsRes, statsRes] = await Promise.allSettled([
        api.fetchFlights('live'),
        api.fetchAlerts('live', { activeOnly: false }),
        api.fetchStats(),
      ]);
      if (!current()) return;
      const flightsPushed = pushed.flights !== before.flights;
      if (!flightsPushed) {
        if (flightsRes.status === 'fulfilled') {
          const merged = mergeLiveFlights(flightsRef.current, flightsRes.value);
          commitFlights(merged);
          setFlightsError(null);
          callbacksRef.current.onFlights?.(merged);
        } else {
          console.error('Failed to load live flights.', flightsRes.reason);
          setFlightsError(describeError(flightsRes.reason, 'Failed to load live flights.'));
        }
      }
      if (alertsRes.status === 'fulfilled') {
        // Episodes merge ("ended" is terminal), so the full list is worth
        // folding in even after a push; prune against the newest membership.
        let next = mergeAlertsByEpisode(alertsRef.current, alertsRes.value);
        if (flightsPushed || flightsRes.status === 'fulfilled') {
          next = pruneUntracked(next, new Set(flightsRef.current.map((f) => f.flight_id)));
        }
        commitAlerts(next);
        setAlertsError(null);
      } else if (pushed.alerts === before.alerts) {
        console.error('Failed to load live alerts.', alertsRes.reason);
        setAlertsError(describeError(alertsRes.reason, 'Failed to load live alerts.'));
      }
      if (statsRes.status === 'fulfilled' && statsRes.value && pushed.stats === before.stats) {
        setStats(statsRes.value);
      }
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
            pushed.stats += 1;
            setStats(message.stats);
            break;
          case 'flights': {
            pushed.flights += 1;
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
            pushed.alerts += 1;
            const incoming = message.alerts;
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
  }, [enabled, retryToken]);

  // Hidden tabs drop flights/telemetry, the bulk of the traffic and of the
  // render/map work. Showing the tab resubscribes; the server then sends a
  // flights snapshot, and onResume backfills telemetry missed meanwhile.
  useEffect(() => {
    if (!enabled) {
      // Nothing outside Live reads live pushes; returning to Live restores
      // them and the server sends fresh flights/alerts snapshots.
      setLiveStreams(IDLE_STREAMS);
      return () => setLiveStreams(null);
    }
    let timer: ReturnType<typeof setTimeout> | null = null;
    let paused = false;
    const update = () => {
      if (document.visibilityState === 'hidden') {
        if (paused || timer) return;
        timer = setTimeout(() => {
          timer = null;
          paused = true;
          setLiveStreams(HIDDEN_STREAMS);
        }, HIDDEN_PAUSE_MS);
        return;
      }
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      if (!paused) return;
      paused = false;
      setLiveStreams(null);
      callbacksRef.current.onResume?.();
    };
    update();
    document.addEventListener('visibilitychange', update);
    return () => {
      document.removeEventListener('visibilitychange', update);
      if (timer) clearTimeout(timer);
      if (paused) setLiveStreams(null);
    };
  }, [enabled]);

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
