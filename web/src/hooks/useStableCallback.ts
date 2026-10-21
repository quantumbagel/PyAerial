import { useCallback, useLayoutEffect, useRef } from 'react';

/**
 * Returns a function with a permanently stable identity that always calls the
 * latest `fn`. Use for callbacks handed to effects or memoised children so
 * they don't re-run/re-render every time the parent renders.
 *
 * The returned function must not be called during render.
 */
export function useStableCallback<A extends unknown[], R>(
  fn: (...args: A) => R,
): (...args: A) => R {
  const ref = useRef(fn);
  useLayoutEffect(() => {
    ref.current = fn;
  });
  return useCallback((...args: A) => ref.current(...args), []);
}

/** A ref that always holds the latest value (updated after commit). */
export function useLatestRef<T>(value: T): React.RefObject<T> {
  const ref = useRef(value);
  useLayoutEffect(() => {
    ref.current = value;
  });
  return ref;
}
