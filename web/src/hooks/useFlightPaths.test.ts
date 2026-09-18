import { describe, expect, it } from 'vitest';
import { pathNeedsFetch } from './useFlightPaths';

describe('pathNeedsFetch', () => {
  const empty = new Set<string>();

  it('queues ids that have never been fetched', () => {
    expect(pathNeedsFetch('a', {}, empty, empty, empty)).toBe(true);
  });

  it('treats a successful empty path as terminal', () => {
    expect(pathNeedsFetch('a', { a: [] }, empty, empty, empty)).toBe(false);
  });

  it('treats fetched ids as terminal even if coords were dropped', () => {
    expect(pathNeedsFetch('a', {}, empty, empty, new Set(['a']))).toBe(false);
  });

  it('does not re-queue pending or failed ids', () => {
    expect(pathNeedsFetch('a', {}, new Set(['a']), empty, empty)).toBe(false);
    expect(pathNeedsFetch('a', {}, empty, new Set(['a']), empty)).toBe(false);
  });

  it('still fetches other ids when one empty success is parked', () => {
    expect(pathNeedsFetch('b', { a: [] }, empty, empty, new Set(['a']))).toBe(true);
  });
});
