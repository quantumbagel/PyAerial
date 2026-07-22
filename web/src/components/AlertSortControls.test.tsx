import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Alert } from '../api/types';
import { AlertSortControls } from './AlertSortControls';
import { Sidebar } from './Sidebar';

describe('AlertSortControls', () => {
  const defaultProps = {
    alertSortField: 'activated' as const,
    alertSortDirection: 'desc' as const,
    onAlertSortChange: vi.fn(),
    onAlertSortDirectionToggle: vi.fn(),
  };

  it('renders the selected sort category label', () => {
    render(<AlertSortControls {...defaultProps} />);
    expect(screen.getByRole('button', { name: /Activated/i })).not.toBeNull();
  });

  it('updates the displayed category when alertSortField changes', () => {
    const { rerender } = render(<AlertSortControls {...defaultProps} />);
    expect(screen.getByRole('button', { name: /Activated/i })).not.toBeNull();

    rerender(<AlertSortControls {...defaultProps} alertSortField="zone" />);
    expect(screen.getByRole('button', { name: /Zone/i })).not.toBeNull();
  });

  it('calls onAlertSortChange when a menu option is selected', () => {
    const onAlertSortChange = vi.fn();
    render(<AlertSortControls {...defaultProps} onAlertSortChange={onAlertSortChange} />);

    fireEvent.click(screen.getByRole('button', { name: /Activated/i }));
    fireEvent.click(screen.getByRole('option', { name: 'Duration' }));

    expect(onAlertSortChange).toHaveBeenCalledWith('duration');
  });
});

describe('Sidebar alert sorting', () => {
  const alerts: Alert[] = [
    {
      alert_id: 'a1',
      flight_id: 'f1',
      zone: 'beta',
      rule: 'warn',
      activated_at: 100,
      active: true,
    },
    {
      alert_id: 'a2',
      flight_id: 'f2',
      zone: 'alpha',
      rule: 'alert',
      activated_at: 200,
      active: false,
      deactivated_at: 300,
    },
  ];

  const baseProps = {
    portalView: 'live' as const,
    sidebarTab: 'alerts' as const,
    searchQuery: '',
    flights: [],
    alerts,
    allAlerts: alerts,
    activeFlightId: null,
    activeAlertId: null,
    flightCount: 0,
    flightSortField: 'last_seen' as const,
    flightSortDirection: 'desc' as const,
    alertSortField: 'zone' as const,
    alertSortDirection: 'asc' as const,
    onSwitchPortalView: vi.fn(),
    onFlightSortChange: vi.fn(),
    onFlightSortDirectionToggle: vi.fn(),
    onAlertSortChange: vi.fn(),
    onAlertSortDirectionToggle: vi.fn(),
    onSwitchSidebarTab: vi.fn(),
    onSearchChange: vi.fn(),
    onSelectFlight: vi.fn(),
    onSelectAlert: vi.fn(),
    onAlertsScroll: vi.fn(),
  };

  it('reorders alerts immediately when sort field changes', () => {
    const { rerender } = render(<Sidebar {...baseProps} />);
    const items = () => Array.from(document.querySelectorAll('#alert-list .alert-item'));

    expect(items()[0].textContent).toContain('alpha');
    expect(items()[1].textContent).toContain('beta');

    rerender(<Sidebar {...baseProps} alertSortField="activated" alertSortDirection="asc" />);

    expect(items()[0].textContent).toContain('beta');
    expect(items()[1].textContent).toContain('alpha');
  });
});
