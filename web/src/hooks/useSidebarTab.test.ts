import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { useSidebarTab } from './useSidebarTab';

describe('useSidebarTab', () => {
  it('counts activations only while the alerts tab is hidden', () => {
    const { result } = renderHook(() => useSidebarTab());
    expect(result.current.tab).toBe('flights');
    expect(result.current.unread).toBe(0);

    act(() => result.current.noteActivatedAlerts(2));
    expect(result.current.unread).toBe(2);

    act(() => result.current.switchTab('alerts'));
    expect(result.current.tab).toBe('alerts');
    expect(result.current.unread).toBe(0);

    act(() => result.current.noteActivatedAlerts(3));
    expect(result.current.unread).toBe(0);
  });

  it('clears the badge when switching views, but keeps it when opening another non-alerts tab', () => {
    const { result } = renderHook(() => useSidebarTab());
    act(() => result.current.noteActivatedAlerts(1));
    act(() => result.current.switchTab('raw'));
    expect(result.current.unread).toBe(1);

    act(() => result.current.resetUnread());
    expect(result.current.unread).toBe(0);
  });
});
