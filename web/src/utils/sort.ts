import { readStoredJson, writeStored } from './storage';

export type SortDirection = 'asc' | 'desc';

export type SortValue = string | number | null | undefined;

export interface SortPreference<K extends string> {
  field: K;
  direction: SortDirection;
}

/** One sortable column: how to read it, which way it starts, and optional row text. */
export interface SortField<T, K extends string = string> {
  id: K;
  label: string;
  defaultDirection: SortDirection;
  /** Return null/undefined/NaN/'' for missing values (they always sort last). */
  value: (item: T) => SortValue;
  display?: (item: T) => string;
}

function hasSortValue(value: SortValue): value is string | number {
  if (value == null || value === '') return false;
  return typeof value === 'number' ? Number.isFinite(value) : true;
}

function compareValues(a: string | number, b: string | number): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b), undefined, { sensitivity: 'base' });
}

/** Missing values always last, both directions. Equal values keep their original order. */
export function sortBy<T, K extends string>(
  items: T[],
  fields: readonly SortField<T, K>[],
  field: K,
  direction: SortDirection,
): T[] {
  const get = fields.find((f) => f.id === field)?.value ?? (() => null);
  const mult = direction === 'asc' ? 1 : -1;
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => {
      const va = get(a.item);
      const vb = get(b.item);
      if (!hasSortValue(va) && !hasSortValue(vb)) return a.index - b.index;
      if (!hasSortValue(va)) return 1;
      if (!hasSortValue(vb)) return -1;
      const cmp = compareValues(va, vb);
      if (cmp !== 0) return cmp * mult;
      return a.index - b.index;
    })
    .map(({ item }) => item);
}

export function defaultSortDirection<T, K extends string>(
  fields: readonly SortField<T, K>[],
  field: K,
): SortDirection {
  return fields.find((f) => f.id === field)?.defaultDirection ?? 'desc';
}

export function loadSort<T, K extends string>(
  storageKey: string,
  fields: readonly SortField<T, K>[],
  defaultField: K,
): SortPreference<K> {
  const parsed = readStoredJson<{ field?: string; direction?: string }>(storageKey);
  const known = fields.some((f) => f.id === parsed?.field);
  const field = known ? (parsed!.field as K) : defaultField;
  const direction =
    known && (parsed?.direction === 'asc' || parsed?.direction === 'desc')
      ? parsed.direction
      : defaultSortDirection(fields, field);
  return { field, direction };
}

export function saveSort<K extends string>(storageKey: string, field: K, direction: SortDirection): void {
  writeStored(storageKey, JSON.stringify({ field, direction }));
}

export function sortValueLabel<T, K extends string>(
  fields: readonly SortField<T, K>[],
  field: K,
  item: T,
  fallback: string,
): string {
  return fields.find((f) => f.id === field)?.display?.(item) ?? fallback;
}
