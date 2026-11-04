import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RawSocketHandlers } from '../api/rawSocket';
import { rawCloseStatus, useRawFrames } from './useRawFrames';

const raw = vi.hoisted(() => ({ handlers: null as RawSocketHandlers | null }));
vi.mock('../api/rawSocket', () => ({
  connectRawSocket: (handlers: RawSocketHandlers) => {
    raw.handlers = handlers;
    return () => {
      raw.handlers = null;
    };
  },
}));

beforeEach(() => {
  raw.handlers = null;
});

describe('useRawFrames', () => {
  it('maps close codes to panel states', () => {
    expect(rawCloseStatus(1013)).toBe('busy');
    expect(rawCloseStatus(1008)).toBe('rejected');
    expect(rawCloseStatus(1006)).toBe('reconnecting');
    expect(rawCloseStatus(undefined)).toBe('reconnecting');
  });

  it('reports the server connection limit as busy, then connected again', () => {
    const { result } = renderHook(() => useRawFrames(true));
    act(() => raw.handlers!.onClose?.(1013, 'server connection limit reached'));
    expect(result.current.status).toBe('busy');
    act(() => raw.handlers!.onOpen?.());
    expect(result.current.status).toBe('connected');
  });

  it('starts each new subscription as connecting, not with the last status', () => {
    const { result, rerender } = renderHook(({ enabled }) => useRawFrames(enabled), {
      initialProps: { enabled: true },
    });
    act(() => raw.handlers!.onOpen?.());
    expect(result.current.status).toBe('connected');
    rerender({ enabled: false });
    expect(raw.handlers).toBeNull();
    rerender({ enabled: true });
    expect(result.current.status).toBe('connecting');
  });
});
