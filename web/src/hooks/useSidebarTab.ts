import { useCallback, useReducer } from 'react';
import type { SidebarTab } from '../components/Sidebar';

type State = { tab: SidebarTab; unread: number };

type Action =
  | { type: 'switchTab'; tab: SidebarTab }
  | { type: 'alertsActivated'; count: number }
  | { type: 'resetUnread' };

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'switchTab':
      return { tab: action.tab, unread: action.tab === 'alerts' ? 0 : state.unread };
    case 'alertsActivated':
      if (action.count <= 0 || state.tab === 'alerts') return state;
      return { ...state, unread: state.unread + action.count };
    case 'resetUnread':
      return state.unread === 0 ? state : { ...state, unread: 0 };
  }
}

/** Sidebar tab plus the unread-alert badge; counting lives here, not in the live feed. */
export function useSidebarTab() {
  const [state, dispatch] = useReducer(reducer, { tab: 'flights' as SidebarTab, unread: 0 });

  const switchTab = useCallback((tab: SidebarTab) => {
    dispatch({ type: 'switchTab', tab });
  }, []);

  const noteActivatedAlerts = useCallback((count: number) => {
    dispatch({ type: 'alertsActivated', count });
  }, []);

  const resetUnread = useCallback(() => {
    dispatch({ type: 'resetUnread' });
  }, []);

  return {
    tab: state.tab,
    unread: state.unread,
    switchTab,
    noteActivatedAlerts,
    resetUnread,
  };
}
