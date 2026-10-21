import { describe, expect, it } from 'vitest';
import { appendCapped, insertByTimestamp, trimToCap } from './buffers';

describe('appendCapped', () => {
  it('appends below the cap', () => {
    expect(appendCapped([1, 2], 3, 5)).toEqual([1, 2, 3]);
  });

  it('never exceeds the cap, even when the input is already over it', () => {
    const long = Array.from({ length: 1000 }, (_, i) => i);
    const next = appendCapped(long, 1000, 400);
    expect(next).toHaveLength(400);
    expect(next[0]).toBe(601);
    expect(next[399]).toBe(1000);
  });

  it('does not mutate the input', () => {
    const input = [1, 2, 3];
    appendCapped(input, 4, 3);
    expect(input).toEqual([1, 2, 3]);
  });
});

describe('trimToCap', () => {
  it('keeps the newest items', () => {
    expect(trimToCap([1, 2, 3, 4], 2)).toEqual([3, 4]);
    const short = [1];
    expect(trimToCap(short, 2)).toBe(short);
  });
});

describe('insertByTimestamp', () => {
  const p = (timestamp: number, tag = '') => ({ timestamp, tag });

  it('appends newer points and skips duplicates', () => {
    const existing = [p(1), p(2)];
    const next = insertByTimestamp(existing, [p(2, 'dup'), p(3)], 10);
    expect(next.map((x) => x.timestamp)).toEqual([1, 2, 3]);
    expect(next[1].tag).toBe('');
  });

  it('inserts late points in order without a full sort', () => {
    const next = insertByTimestamp([p(1), p(5)], [p(3), p(0), p(9)], 10);
    expect(next.map((x) => x.timestamp)).toEqual([0, 1, 3, 5, 9]);
  });

  it('returns the same array when nothing changes', () => {
    const existing = [p(1)];
    expect(insertByTimestamp(existing, [p(1)], 10)).toBe(existing);
    expect(insertByTimestamp(existing, [], 10)).toBe(existing);
  });

  it('caps to the newest points', () => {
    const next = insertByTimestamp([p(1), p(2), p(3)], [p(4), p(5)], 3);
    expect(next.map((x) => x.timestamp)).toEqual([3, 4, 5]);
  });
});
