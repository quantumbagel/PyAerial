import { useCallback, useState } from 'react';
import type { PortalView } from '../api/types';

export type SortDirection = 'asc' | 'desc';

interface SortPreference<F extends string> {
  field: F;
  direction: SortDirection;
}

interface SortPreferenceStore<F extends string> {
  load: (view: PortalView) => SortPreference<F>;
  save: (view: PortalView, field: F, direction: SortDirection) => void;
  defaultDirection: (field: F) => SortDirection;
}

/** Per-view sort field/direction, persisted (safely) between sessions. */
export function useSortPreference<F extends string>(
  view: PortalView,
  { load, save, defaultDirection }: SortPreferenceStore<F>,
) {
  const [prefs, setPrefs] = useState<Record<PortalView, SortPreference<F>>>(() => ({
    live: load('live'),
    history: load('history'),
  }));
  const current = prefs[view];

  const commit = useCallback(
    (next: SortPreference<F>) => {
      save(view, next.field, next.direction);
      setPrefs((prev) => ({ ...prev, [view]: next }));
    },
    [save, view],
  );

  const setField = useCallback(
    (field: F) => commit({ field, direction: defaultDirection(field) }),
    [commit, defaultDirection],
  );

  const toggleDirection = useCallback(
    () =>
      commit({
        field: current.field,
        direction: current.direction === 'asc' ? 'desc' : 'asc',
      }),
    [commit, current.field, current.direction],
  );

  return { field: current.field, direction: current.direction, setField, toggleDirection };
}
