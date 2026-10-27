import { useCallback, useState } from 'react';
import type { PortalView } from '../api/types';
import {
  defaultSortDirection,
  loadSort,
  saveSort,
  type SortField,
  type SortPreference,
} from '../utils/sort';

/** Per-view sort field/direction, persisted between sessions. */
export function useSortPreference<T, F extends string>(
  view: PortalView,
  storagePrefix: string,
  fields: readonly SortField<T, F>[],
  defaultField: F,
) {
  const [prefs, setPrefs] = useState<Record<PortalView, SortPreference<F>>>(() => ({
    live: loadSort(`${storagePrefix}:live`, fields, defaultField),
    history: loadSort(`${storagePrefix}:history`, fields, defaultField),
  }));
  const current = prefs[view];

  const commit = useCallback(
    (next: SortPreference<F>) => {
      saveSort(`${storagePrefix}:${view}`, next.field, next.direction);
      setPrefs((prev) => ({ ...prev, [view]: next }));
    },
    [storagePrefix, view],
  );

  const setField = useCallback(
    (field: F) => commit({ field, direction: defaultSortDirection(fields, field) }),
    [commit, fields],
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
