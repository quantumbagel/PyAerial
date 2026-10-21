import type { PortalView, ServerStats } from '../api/types';

/** Keep in sync with pyaerial.constants.LIVE_ENGINE_TTL_SECONDS */
export const ENGINE_STALE_SECONDS = 10;

/** Shared copy for a stopped tracker. */
export const ENGINE_DOWN = 'The tracking engine is not running. Start it with "pyaerial run".';
export const ENGINE_IDLE_BANNER =
  'The tracking engine is idle. Start it with "pyaerial run"; frozen aircraft will expire shortly.';
export const REDIS_DOWN = 'The live store (Redis) is unreachable, so the portal cannot see the tracker.';

/**
 * Whether the tracking engine is heartbeating. Prefers the server's own
 * verdict (`engine_live`), so a skewed browser clock can't flag a healthy
 * engine as idle; falls back to comparing `engine_seen_at` with our clock for
 * older servers.
 */
export function isEngineLive(
  stats: ServerStats | null | undefined,
  nowSeconds = Date.now() / 1000,
): boolean {
  if (typeof stats?.engine_live === 'boolean') return stats.engine_live;
  const seen = stats?.engine_seen_at;
  if (seen == null || !Number.isFinite(seen)) return false;
  return nowSeconds - seen < ENGINE_STALE_SECONDS;
}

export function emptyFlightsMessage({
  view,
  hasFilters,
  stats,
}: {
  view: PortalView;
  hasFilters: boolean;
  stats: ServerStats | null;
}): string {
  if (hasFilters) return 'No flights match your filters.';
  if (view === 'history') {
    if (stats?.history === false) {
      return 'History is unavailable: the archive is not connected.';
    }
    return 'No archived flights yet. A flight is archived only when a retain rule holds for its dwell time.';
  }
  if (stats?.redis === false) {
    return REDIS_DOWN;
  }
  if (stats != null && !isEngineLive(stats)) {
    return `${ENGINE_DOWN} Make sure dump1090 is feeding it.`;
  }
  return 'No aircraft in range right now.';
}

export function emptyAlertsMessage({
  view,
  hasFilters,
  stats,
}: {
  view: PortalView;
  hasFilters: boolean;
  stats: ServerStats | null;
}): string {
  if (hasFilters) return 'No alerts match your filters.';
  if (view === 'history') {
    if (stats?.history === false) {
      return 'History is unavailable: the archive is not connected.';
    }
    return 'No archived alerts yet.';
  }
  if (stats?.redis === false) {
    return REDIS_DOWN;
  }
  if (stats != null && !isEngineLive(stats)) {
    return ENGINE_DOWN;
  }
  return 'No alerts yet.';
}
