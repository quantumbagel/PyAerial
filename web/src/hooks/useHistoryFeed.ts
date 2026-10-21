import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../api/client';
import type { Alert, FlightSummary, ServerStats } from '../api/types';
import { alertEpisodeIdentity, dedupeAlerts, mergeAlertsByEpisode } from '../utils/alertData';

export const HISTORY_PAGE_LIMIT = 50;
export const HISTORY_REFRESH_MS = 10_000;

export interface HistoryFilter {
  q: string;
  since: number | null;
  until: number | null;
}

type Kind = 'flights' | 'alerts';

/**
 * Merge a freshly fetched first page into an already-paginated list: entries
 * present in both are replaced, new ones are prepended. Returns the merged
 * list and how many entries were new (so pagination offsets can shift).
 */
export function mergeFirstPage<T>(
  existing: T[],
  page: T[],
  keyOf: (item: T) => string,
  merge: (current: T[], updates: T[]) => T[] = (current, updates) => {
    const byKey = new Map(updates.map((u) => [keyOf(u), u]));
    return current.map((item) => byKey.get(keyOf(item)) ?? item);
  },
): { items: T[]; added: number } {
  if (!existing.length) return { items: page, added: page.length };
  const known = new Set(existing.map(keyOf));
  const fresh = page.filter((item) => !known.has(keyOf(item)));
  const updated = merge(existing, page.filter((item) => known.has(keyOf(item))));
  return { items: fresh.length ? [...fresh, ...updated] : updated, added: fresh.length };
}

const flightKey = (f: FlightSummary) => f.flight_id;

/**
 * Archived flights/alerts with server-side filtering and infinite scroll.
 *
 * Each filter change (or view switch) starts a new "generation": responses
 * from older generations are discarded, so a slow request can never overwrite
 * newer results. Periodic refreshes only re-read the first page and merge it.
 */
export function useHistoryFeed(enabled: boolean, filter: HistoryFilter) {
  const [flights, setFlights] = useState<FlightSummary[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [stats, setStats] = useState<ServerStats | null>(null);
  const [isLoadingFlights, setIsLoadingFlights] = useState(true);
  const [isLoadingAlerts, setIsLoadingAlerts] = useState(true);
  const [flightsError, setFlightsError] = useState<string | null>(null);
  const [alertsError, setAlertsError] = useState<string | null>(null);

  const generation = useRef(0);
  const filterRef = useRef(filter);
  const lists = useRef({ flights: [] as FlightSummary[], alerts: [] as Alert[] });
  const fetched = useRef({ flights: 0, alerts: 0 });
  const hasMore = useRef({ flights: true, alerts: true });
  /** Generation of the in-flight request per kind, or null. */
  const inFlight = useRef<Record<Kind, number | null>>({ flights: null, alerts: null });

  const { q, since, until } = filter;

  const listOpts = useCallback((): api.HistoryListOpts => {
    const f = filterRef.current;
    const opts: api.HistoryListOpts = { limit: HISTORY_PAGE_LIMIT };
    if (f.q) opts.q = f.q;
    if (f.since != null) opts.since = f.since;
    if (f.until != null) opts.until = f.until;
    return opts;
  }, []);

  const commit = useCallback((kind: Kind, items: FlightSummary[] | Alert[]) => {
    if (kind === 'flights') {
      lists.current.flights = items as FlightSummary[];
      setFlights(items as FlightSummary[]);
    } else {
      lists.current.alerts = items as Alert[];
      setAlerts(items as Alert[]);
    }
  }, []);

  const setLoading = useCallback((kind: Kind, value: boolean) => {
    if (kind === 'flights') setIsLoadingFlights(value);
    else setIsLoadingAlerts(value);
  }, []);

  const setError = useCallback((kind: Kind, value: string | null) => {
    if (kind === 'flights') setFlightsError(value);
    else setAlertsError(value);
  }, []);

  /** Fetch (or re-fetch) the first page of one list and merge it in. */
  const refreshKind = useCallback(
    async (kind: Kind, gen: number) => {
      if (inFlight.current[kind] === gen) return;
      inFlight.current[kind] = gen;
      try {
        const opts = { ...listOpts(), skip: 0 };
        if (kind === 'flights') {
          const page = await api.fetchFlights('history', opts);
          if (gen !== generation.current) return;
          const { items, added } = mergeFirstPage(lists.current.flights, page, flightKey);
          const initial = fetched.current.flights === 0;
          fetched.current.flights += added;
          if (initial) hasMore.current.flights = page.length >= HISTORY_PAGE_LIMIT;
          commit('flights', items);
        } else {
          const page = dedupeAlerts(await api.fetchAlerts('history', opts));
          if (gen !== generation.current) return;
          const { items, added } = mergeFirstPage(
            lists.current.alerts,
            page,
            alertEpisodeIdentity,
            mergeAlertsByEpisode,
          );
          const initial = fetched.current.alerts === 0;
          fetched.current.alerts += added;
          if (initial) hasMore.current.alerts = page.length >= HISTORY_PAGE_LIMIT;
          commit('alerts', items);
        }
        setError(kind, null);
      } catch (err) {
        if (gen !== generation.current) return;
        console.error(`Failed to load ${kind}.`, err);
        // A failed background refresh keeps what is already on screen.
        if (!lists.current[kind].length) setError(kind, `Failed to load ${kind}.`);
      } finally {
        if (inFlight.current[kind] === gen) inFlight.current[kind] = null;
        if (gen === generation.current) setLoading(kind, false);
      }
    },
    [commit, listOpts, setError, setLoading],
  );

  const refreshStats = useCallback(async (gen: number) => {
    try {
      const next = await api.fetchStats();
      if (gen === generation.current && next) setStats(next);
    } catch {
      // stats are decorative here
    }
  }, []);

  useEffect(() => {
    if (!enabled) return undefined;
    filterRef.current = { q, since, until };
    const gen = ++generation.current;
    lists.current = { flights: [], alerts: [] };
    fetched.current = { flights: 0, alerts: 0 };
    hasMore.current = { flights: true, alerts: true };
    setFlights([]);
    setAlerts([]);
    setFlightsError(null);
    setAlertsError(null);
    setIsLoadingFlights(true);
    setIsLoadingAlerts(true);

    const refreshAll = () => {
      void refreshKind('flights', gen);
      void refreshKind('alerts', gen);
      void refreshStats(gen);
    };
    refreshAll();
    const timer = setInterval(refreshAll, HISTORY_REFRESH_MS);
    return () => {
      clearInterval(timer);
      // Invalidate anything still in flight for this generation.
      generation.current += 1;
    };
  }, [enabled, q, since, until, refreshKind, refreshStats]);

  /** Load the next page of one list (infinite scroll). */
  const loadMore = useCallback(
    async (kind: Kind) => {
      const gen = generation.current;
      if (!hasMore.current[kind] || inFlight.current[kind] === gen) return;
      inFlight.current[kind] = gen;
      try {
        const opts = { ...listOpts(), skip: fetched.current[kind] };
        if (kind === 'flights') {
          const data = await api.fetchFlights('history', opts);
          if (gen !== generation.current) return;
          fetched.current.flights += data.length;
          hasMore.current.flights = data.length >= HISTORY_PAGE_LIMIT;
          const seen = new Set(lists.current.flights.map(flightKey));
          const extra = data.filter((f) => !seen.has(f.flight_id));
          if (extra.length) commit('flights', [...lists.current.flights, ...extra]);
        } else {
          // Paginate on the server's count, not the deduped client list.
          const data = await api.fetchAlerts('history', opts);
          if (gen !== generation.current) return;
          fetched.current.alerts += data.length;
          hasMore.current.alerts = data.length >= HISTORY_PAGE_LIMIT;
          if (data.length) commit('alerts', dedupeAlerts([...lists.current.alerts, ...data]));
        }
      } catch (err) {
        console.error(`Failed to load more ${kind}.`, err);
      } finally {
        if (inFlight.current[kind] === gen) inFlight.current[kind] = null;
      }
    },
    [commit, listOpts],
  );

  const retry = useCallback(
    (kind: Kind) => {
      setError(kind, null);
      setLoading(kind, true);
      void refreshKind(kind, generation.current);
    },
    [refreshKind, setError, setLoading],
  );

  return {
    flights,
    alerts,
    stats,
    isLoadingFlights,
    isLoadingAlerts,
    flightsError,
    alertsError,
    loadMore,
    retry,
  };
}
