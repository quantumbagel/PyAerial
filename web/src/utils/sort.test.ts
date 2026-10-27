import { afterEach, describe, expect, it } from 'vitest';
import { loadSort, saveSort, sortBy, type SortField } from './sort';
import { clearStoredMemory, writeStored } from './storage';

type Item = { id: string; n?: number | null; s?: string | null };

const fields: readonly SortField<Item, 'n' | 's'>[] = [
  { id: 'n', label: 'Number', defaultDirection: 'desc', value: (item) => item.n },
  { id: 's', label: 'Text', defaultDirection: 'asc', value: (item) => item.s },
];

afterEach(() => {
  clearStoredMemory();
  window.localStorage?.clear();
});

describe('sortBy', () => {
  it('places missing values last when descending', () => {
    const items: Item[] = [
      { id: 'a', n: 1 },
      { id: 'b', n: null },
      { id: 'c', n: 3 },
      { id: 'd' },
    ];
    expect(sortBy(items, fields, 'n', 'desc').map((item) => item.id)).toEqual(['c', 'a', 'b', 'd']);
  });

  it('places missing values last when ascending', () => {
    const items: Item[] = [
      { id: 'a', n: 1 },
      { id: 'b', n: null },
      { id: 'c', n: 3 },
      { id: 'd', n: Number.NaN },
    ];
    expect(sortBy(items, fields, 'n', 'asc').map((item) => item.id)).toEqual(['a', 'c', 'b', 'd']);
  });

  it('breaks ties by original order in both directions', () => {
    const items: Item[] = [
      { id: 'a', n: 1 },
      { id: 'b', n: 1 },
      { id: 'c', n: 1 },
    ];
    expect(sortBy(items, fields, 'n', 'desc').map((item) => item.id)).toEqual(['a', 'b', 'c']);
    expect(sortBy(items, fields, 'n', 'asc').map((item) => item.id)).toEqual(['a', 'b', 'c']);
  });

  it('compares text case-insensitively and treats empty as missing', () => {
    const items: Item[] = [
      { id: 'a', s: 'bravo' },
      { id: 'b', s: '' },
      { id: 'c', s: 'Alpha' },
    ];
    expect(sortBy(items, fields, 's', 'asc').map((item) => item.id)).toEqual(['c', 'a', 'b']);
    expect(sortBy(items, fields, 's', 'desc').map((item) => item.id)).toEqual(['a', 'c', 'b']);
  });
});

describe('loadSort', () => {
  it('falls back to the default field when the stored field is unknown', () => {
    writeStored('sort:unknown-field', JSON.stringify({ field: 'nope', direction: 'asc' }));
    expect(loadSort('sort:unknown-field', fields, 'n')).toEqual({ field: 'n', direction: 'desc' });
  });

  it('falls back to the field default when direction is invalid', () => {
    writeStored('sort:bad-dir', JSON.stringify({ field: 's', direction: 'sideways' }));
    expect(loadSort('sort:bad-dir', fields, 'n')).toEqual({ field: 's', direction: 'asc' });
  });

  it('returns defaults for missing keys and bad JSON', () => {
    expect(loadSort('sort:missing', fields, 'n')).toEqual({ field: 'n', direction: 'desc' });
    writeStored('sort:bad-json', '{not json');
    expect(loadSort('sort:bad-json', fields, 'n')).toEqual({ field: 'n', direction: 'desc' });
  });

  it('returns a stored preference when field and direction are valid', () => {
    saveSort('sort:ok', 's', 'desc');
    expect(loadSort('sort:ok', fields, 'n')).toEqual({ field: 's', direction: 'desc' });
  });
});
