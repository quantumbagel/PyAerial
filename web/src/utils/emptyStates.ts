import type { PortalView, ServerStats } from '../api/types';

/** Keep in sync with pyaerial.constants.LIVE_ENGINE_TTL_SECONDS */
export const ENGINE_STALE_SECONDS = 10;

export function isEngineLive(
  stats: ServerStats | null | undefined,
  nowSeconds = Date.now() / 1000,
): boolean {
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
    if (stats?.mongo === false) {
      return 'Historical data is unavailable (MongoDB not connected).';
    }
    return 'No retained flights yet. Completed flights are archived only when a retain rule holds for its dwell time.';
  }
  if (stats?.redis === false) {
    return 'Live store (Redis) is unreachable. The portal cannot see the tracker.';
  }
  if (stats != null && !isEngineLive(stats)) {
    return 'Tracking engine is not running. Start `pyaerial run` and confirm dump1090 is feeding it.';
  }
  return 'No aircraft on the live feed.';
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
    if (stats?.mongo === false) {
      return 'Historical alerts are unavailable (MongoDB not connected).';
    }
    return 'No retained alerts yet.';
  }
  if (stats?.redis === false) {
    return 'Live store (Redis) is unreachable. The portal cannot see the tracker.';
  }
  if (stats != null && !isEngineLive(stats)) {
    return 'Tracking engine is not running. Start `pyaerial run`.';
  }
  return 'No geofence alerts yet.';
}
