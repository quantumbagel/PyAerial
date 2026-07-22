import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Alert, FlightSummary } from '../api/types';
import { isAlertActive } from '../utils/format';
import { AlertListItem } from './AlertListItem';
import { AlertToasts } from './AlertToasts';
import { AlertStatusBadge, LevelBadge } from './LevelBadge';
import { Sidebar } from './Sidebar';

describe('Alert status & episode display enhancements', () => {
  it('correctly determines if an alert episode is active or ended', () => {
    const activeAlert: Alert = {
      alert_id: 'a1',
      active: true,
      activated_at: 1700000000,
      deactivated_at: null,
    };
    const endedAlert: Alert = {
      alert_id: 'a2',
      active: false,
      activated_at: 1700000000,
      deactivated_at: 1700000300,
    };

    expect(isAlertActive(activeAlert)).toBe(true);
    expect(isAlertActive(endedAlert)).toBe(false);
  });

  it('renders AlertStatusBadge with LIVE for active and ENDED for deactivated', () => {
    const { rerender } = render(<AlertStatusBadge active={true} />);
    expect(screen.getByText('LIVE')).not.toBeNull();

    rerender(<AlertStatusBadge active={false} />);
    expect(screen.getByText('ENDED')).not.toBeNull();
  });

  it('renders LevelBadge as Clear (done) when flight has 0 active alerts despite having episode stats history', () => {
    const flightHistoryOnly: FlightSummary = {
      flight_id: 'f1',
      icao: 'A1B2C3',
      is_live: true,
      active_alerts: [],
      alert_stats: { episode_count: 2, total_seconds: 1200, active_count: 0 },
    };

    const { container } = render(<LevelBadge flight={flightHistoryOnly} />);
    expect(screen.getByText('2 episodes · 20m alerted')).not.toBeNull();
    expect(container.querySelector('.level-badge.done')).not.toBeNull();
    expect(container.querySelector('.level-badge.warn')).toBeNull();
    expect(container.querySelector('.level-badge.alert')).toBeNull();
  });

  it('renders AlertListItem with LIVE badge for active alert and ENDED badge for ended alert', () => {
    const activeAlert: Alert = {
      alert_id: 'alert_live',
      flight_id: 'f1',
      icao: 'A1B2C3',
      callsign: 'N123AB',
      zone: 'aerpaw',
      rule: 'alert',
      active: true,
      activated_at: 1700000000,
      altitude: 1200,
      eta: 45,
    };

    const endedAlert: Alert = {
      alert_id: 'alert_ended',
      flight_id: 'f2',
      icao: 'B4C5D6',
      callsign: 'DRONE01',
      zone: 'aerpaw',
      rule: 'warn',
      active: false,
      activated_at: 1700000000,
      deactivated_at: 1700000120,
      altitude: 500,
      eta: null,
    };

    const onSelect = vi.fn();
    const { rerender } = render(<AlertListItem alert={activeAlert} active={false} onSelect={onSelect} />);

    expect(screen.getByText('LIVE')).not.toBeNull();
    expect(screen.getByText('N123AB')).not.toBeNull();

    rerender(<AlertListItem alert={endedAlert} active={false} onSelect={onSelect} />);

    expect(screen.getByText('ENDED')).not.toBeNull();
    expect(screen.getByText('DRONE01')).not.toBeNull();
  });

  it('renders AlertToasts with LIVE and CLEARED status for active and deactivated events', () => {
    const activeAlert: Alert = {
      alert_id: 'a10',
      callsign: 'TEST01',
      zone: 'testzone',
      rule: 'warn',
      active: true,
      activated_at: 1700000000,
    };
    const clearedAlert: Alert = {
      ...activeAlert,
      active: false,
      deactivated_at: 1700000100,
    };

    const toasts = [
      { id: 't1', alert: activeAlert, eventType: 'activated' as const, duration: 6000 },
      { id: 't2', alert: clearedAlert, eventType: 'deactivated' as const, duration: 5000 },
    ];

    render(<AlertToasts toasts={toasts} onSelectAlert={vi.fn()} onDismiss={vi.fn()} />);

    expect(screen.getByText('LIVE')).not.toBeNull();
    expect(screen.getByText('CLEARED')).not.toBeNull();
    expect(screen.getByText('Total Episode: 1m 40s')).not.toBeNull();
  });

  it('renders active alerts count badge on Alerts tab in live view, and omits it in historical view', () => {
    const alerts: Alert[] = [
      { alert_id: 'a1', active: true, activated_at: 1700000000 },
      { alert_id: 'a2', active: false, activated_at: 1700000000, deactivated_at: 1700000100 },
    ];

    const sidebarProps = {
      sidebarTab: 'flights' as const,
      searchQuery: '',
      flights: [],
      alerts,
      allAlerts: alerts,
      activeFlightId: null,
      activeAlertId: null,
      flightCount: 1,
      flightSortField: 'last_seen' as const,
      flightSortDirection: 'desc' as const,
      notificationsEnabled: false,
      onEnableNotifications: vi.fn(),
      onSwitchPortalView: vi.fn(),
      onFlightSortChange: vi.fn(),
      onFlightSortDirectionToggle: vi.fn(),
      onSwitchSidebarTab: vi.fn(),
      onSearchChange: vi.fn(),
      onSelectFlight: vi.fn(),
      onSelectAlert: vi.fn(),
      onAlertsScroll: vi.fn(),
    };

    const { rerender } = render(<Sidebar {...sidebarProps} portalView="live" />);
    const badgeInLive = document.querySelector('.alerts-badge-count');
    expect(badgeInLive).not.toBeNull();
    expect(badgeInLive?.textContent).toBe('1');

    rerender(<Sidebar {...sidebarProps} portalView="history" />);
    const badgeInHistory = document.querySelector('.alerts-badge-count');
    expect(badgeInHistory).toBeNull();
  });
});
