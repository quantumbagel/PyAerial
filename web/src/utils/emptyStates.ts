import type { PortalView, ServerStats, WsStatus } from '../api/types';

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

export type LiveIssue = 'live_unavailable' | 'engine_down';

/**
 * The one verdict on the live pipeline, shared by the banner and every empty
 * state so they can never disagree. An unreachable live store wins: with
 * Redis down the engine heartbeat can't be read either.
 */
export function liveIssue(stats: ServerStats | null | undefined): LiveIssue | null {
  if (stats == null) return null;
  if (stats.redis === false) return 'live_unavailable';
  if (!isEngineLive(stats)) return 'engine_down';
  return null;
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
  const issue = liveIssue(stats);
  if (issue === 'live_unavailable') return REDIS_DOWN;
  if (issue === 'engine_down') return `${ENGINE_DOWN} Make sure dump1090 is feeding it.`;
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
  const issue = liveIssue(stats);
  if (issue === 'live_unavailable') return REDIS_DOWN;
  if (issue === 'engine_down') return ENGINE_DOWN;
  return 'No alerts yet.';
}

export const BUSY_BANNER = 'The server is at its connection limit. Reconnecting…';

export interface StatusBanner {
  text: string;
  /** Show a spinner: the portal is retrying on its own. */
  pending: boolean;
}

/**
 * The top-of-map status line. Uses the same liveIssue verdict as the empty
 * states, in one precedence order: origin rejected > connection state >
 * live store down > engine down > config/zones errors.
 */
export function statusBanner({
  wsStatus,
  view,
  stats,
  rejectedMessage,
  bootstrapError,
}: {
  wsStatus: WsStatus;
  view: PortalView;
  stats: ServerStats | null;
  rejectedMessage?: string | null;
  bootstrapError?: string | null;
}): StatusBanner | null {
  if (wsStatus === 'rejected') return { text: rejectedMessage || 'Access denied.', pending: false };
  if (wsStatus === 'busy') return { text: BUSY_BANNER, pending: true };
  if (wsStatus === 'connecting') return { text: 'Connecting…', pending: true };
  if (wsStatus === 'reconnecting') return { text: 'Reconnecting…', pending: true };
  if (view === 'live') {
    const issue = liveIssue(stats);
    if (issue === 'live_unavailable') return { text: REDIS_DOWN, pending: false };
    if (issue === 'engine_down') return { text: ENGINE_IDLE_BANNER, pending: false };
  }
  return bootstrapError ? { text: bootstrapError, pending: false } : null;
}
