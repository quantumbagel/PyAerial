import { HISTORY_FETCH_CAP } from '../api/client';

export async function fetchHistoryPages<T>(
  want: number,
  load: (skip: number, limit: number) => Promise<T[]>,
  stillCurrent: () => boolean = () => true,
): Promise<{ items: T[]; hasMore: boolean } | null> {
  const items: T[] = [];
  let skip = 0;
  let hasMore = false;
  while (skip < want) {
    if (!stillCurrent()) return null;
    const limit = Math.min(HISTORY_FETCH_CAP, Math.max(1, want - skip));
    const batch = await load(skip, limit);
    items.push(...batch);
    if (batch.length < limit) {
      hasMore = false;
      break;
    }
    hasMore = true;
    skip += batch.length;
  }
  return { items, hasMore };
}
