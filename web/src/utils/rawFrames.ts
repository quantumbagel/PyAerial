import { ORIGIN_REJECTED_MESSAGE } from '../api/errors';
import { EMPTY, formatNumber } from './format';
import type { BufferedRawFrame, RawFrame, ServerStats, WsStatus } from '../api/types';
import { ENGINE_DOWN, REDIS_DOWN, liveIssue } from './emptyStates';

export const RAW_BUFFER_CAP = 400;

/** Incoming batches are oldest-first (wire order). Newest frames end up at the front. */
export function prependRawFrames<T>(
  existing: T[],
  incoming: T[],
  cap = RAW_BUFFER_CAP,
): T[] {
  if (!incoming.length) return existing;
  const newestFirst = incoming.length > 1 ? [...incoming].reverse() : incoming;
  if (newestFirst.length >= cap) return newestFirst.slice(0, cap);
  const keep = cap - newestFirst.length;
  return newestFirst.concat(existing.length > keep ? existing.slice(0, keep) : existing);
}

/** Receivers that heard this frame, strongest first; falls back to `receiver`. */
export function rawReceiverNames(frame: RawFrame): string[] {
  if (frame.receivers?.length) return frame.receivers;
  return frame.receiver ? [frame.receiver] : [];
}

export function rawFrameMatches(frame: RawFrame, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  if ((frame.icao || '').toLowerCase().includes(q)) return true;
  if ((frame.hex || '').toLowerCase().includes(q)) return true;
  if (rawReceiverNames(frame).some((name) => name.toLowerCase().includes(q))) return true;
  if (frame.df != null) {
    const df = String(frame.df);
    if (df === q || `df${df}` === q) return true;
  }
  return false;
}

export function filterRawFrames(
  frames: BufferedRawFrame[],
  query: string,
): BufferedRawFrame[] {
  if (!query.trim()) return frames;
  return frames.filter((frame) => rawFrameMatches(frame, query));
}

export function formatRawDf(df?: number): string {
  return df != null && Number.isFinite(df) ? `DF${df}` : `DF${EMPTY}`;
}

export function formatRawRssi(rssi?: number): string {
  return rssi != null && Number.isFinite(rssi) ? `${formatNumber(rssi, 1)} dBFS` : '';
}

export function emptyRawMessage({
  hasFilters,
  stats,
  rawStatus,
}: {
  hasFilters: boolean;
  stats: ServerStats | null;
  rawStatus: WsStatus;
}): string {
  if (hasFilters) return 'No frames match your filters.';
  if (rawStatus === 'rejected') {
    return ORIGIN_REJECTED_MESSAGE;
  }
  const issue = liveIssue(stats);
  if (issue === 'live_unavailable') return REDIS_DOWN;
  if (issue === 'engine_down') return ENGINE_DOWN;
  return 'No raw frames yet. They appear once dump1090 is feeding the tracker.';
}
