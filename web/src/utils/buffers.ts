/** Bounded, ordered buffers for live path and telemetry data. */

/** Points kept for paths drawn for every visible aircraft ("Paths" toggle). */
export const OVERLAY_POINT_CAP = 400;
/** Points kept for the selected aircraft (path + telemetry table). */
export const SELECTED_POINT_CAP = 5000;

/** Append one item, dropping the oldest so the result never exceeds `cap`. */
export function appendCapped<T>(existing: T[], item: T, cap: number): T[] {
  const keep = Math.max(0, cap - 1);
  const start = existing.length > keep ? existing.length - keep : 0;
  const next = existing.slice(start);
  next.push(item);
  return next;
}

/** Keep the newest `cap` items. Returns the same array when already short enough. */
export function trimToCap<T>(items: T[], cap: number): T[] {
  return items.length > cap ? items.slice(items.length - cap) : items;
}

type Timestamped = { timestamp: number };

/** Index of the first element whose timestamp is >= ts (binary search). */
function lowerBound<T extends Timestamped>(items: T[], ts: number): number {
  let lo = 0;
  let hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if ((items[mid].timestamp || 0) < ts) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * Merge points into a timestamp-sorted array without re-sorting it.
 * Points whose timestamp is already present are ignored. Live points almost
 * always land at the end, so this is O(k) for the common case.
 */
export function insertByTimestamp<T extends Timestamped>(
  existing: T[],
  incoming: T[],
  cap: number,
): T[] {
  if (!incoming.length) return existing;
  let next: T[] | null = null;
  for (const point of incoming) {
    const ts = point.timestamp || 0;
    const arr: T[] = next ?? existing;
    const last = arr[arr.length - 1];
    if (!last || (last.timestamp || 0) < ts) {
      next = next ?? existing.slice();
      next.push(point);
      continue;
    }
    const idx = lowerBound(arr, ts);
    if (idx < arr.length && (arr[idx].timestamp || 0) === ts) continue;
    next = next ?? existing.slice();
    next.splice(idx, 0, point);
  }
  return next ? trimToCap(next, cap) : existing;
}
