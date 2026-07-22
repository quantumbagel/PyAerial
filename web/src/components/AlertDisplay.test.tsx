import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Alert, FlightSummary } from '../api/types';
import { alertEpisodeKey } from '../utils/alertData';
import { isAlertActive } from '../utils/format';
import { AlertListItem } from './AlertListItem';
import { LevelBadge, LiveBadge } from './LevelBadge';
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

  it('renders LiveBadge for active episodes', () => {
    render(<LiveBadge />);
    expect(screen.getByText('Live')).not.toBeNull();
  });

  it('renders LevelBadge with zone badges for live flights with active alerts', () => {
    const flight: FlightSummary = {
      flight_id: 'f1',
      icao: 'A1B2C3',
      is_live: true,
      active_alerts: [{ zone: 'aerpaw', rule: 'warn', activated_at: 1700000000 }],
    };

    const { container } = render(
      <LevelBadge flight={flight} zones={[{ name: 'aerpaw', coordinates: [], rules: [] }]} />,
    );
    expect(screen.queryByText('Live')).toBeNull();
    expect(screen.getByText('aerpaw · warn')).not.toBeNull();
    expect(container.querySelector('.zone-badge')).not.toBeNull();
  });

  it('renders no LevelBadge for live flights without active alerts', () => {
    const flight: FlightSummary = {
      flight_id: 'f1',
      icao: 'A1B2C3',
      is_live: true,
      active_alerts: [],
    };

    const { container } = render(<LevelBadge flight={flight} />);
    expect(screen.queryByText('Live')).toBeNull();
    expect(container.querySelector('.zone-badge')).toBeNull();
    expect(container.querySelector('.level-badge')).toBeNull();
  });

  it('renders LevelBadge as Clear (done) when flight has 0 active alerts despite having episode stats history', () => {
    const flightHistoryOnly: FlightSummary = {
      flight_id: 'f1',
      icao: 'A1B2C3',
      is_live: false,
      active_alerts: [],
      alert_stats: { episode_count: 2, total_seconds: 1200, active_count: 0 },
    };

    const { container } = render(<LevelBadge flight={flightHistoryOnly} />);
    expect(screen.getByText('2 episodes · 20m alerted')).not.toBeNull();
    expect(container.querySelector('.level-badge.done')).not.toBeNull();
    expect(container.querySelector('.zone-badge')).toBeNull();
  });

  it('renders AlertListItem with colored zone text and no Live badge', () => {
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
    const { rerender } = render(
      <AlertListItem alert={activeAlert} episodeKey="alert_live:active" active={false} sortField="activated" onSelect={onSelect} />,
    );

    expect(screen.queryByText('Live')).toBeNull();
    expect(screen.getByText('aerpaw · alert (Live)')).not.toBeNull();
    expect(screen.getByText('N123AB')).not.toBeNull();
    expect(screen.queryByText(/ongoing/i)).toBeNull();

    rerender(
      <AlertListItem alert={endedAlert} episodeKey="alert_ended:ended" active={false} sortField="activated" onSelect={onSelect} />,
    );

    expect(screen.queryByText('Live')).toBeNull();
    expect(screen.getByText('aerpaw · warn')).not.toBeNull();
    expect(screen.getByText('DRONE01')).not.toBeNull();
  });

  it('highlights only the selected episode when multiple rows share the same alert_id', () => {
    const sharedAlertId = 'f1:aerpaw:warn';
    const alerts: Alert[] = [
      {
        alert_id: sharedAlertId,
        flight_id: 'f1',
        icao: 'A1B2C3',
        callsign: 'N123AB',
        zone: 'aerpaw',
        rule: 'warn',
        active: false,
        activated_at: 1700000000,
        deactivated_at: 1700000300,
      },
      {
        alert_id: sharedAlertId,
        flight_id: 'f1',
        icao: 'A1B2C3',
        callsign: 'N123AB',
        zone: 'aerpaw',
        rule: 'warn',
        active: true,
        activated_at: 1700001000,
        deactivated_at: null,
      },
    ];
    const selectedKey = alertEpisodeKey(alerts[1]);

    render(
      <ul>
        {alerts.map((alert, index) => {
          const episodeKey = alertEpisodeKey(alert, index);
          return (
            <AlertListItem
              key={`${episodeKey}:${index}`}
              episodeKey={episodeKey}
              alert={alert}
              active={episodeKey === selectedKey}
              sortField="activated"
              onSelect={vi.fn()}
            />
          );
        })}
      </ul>,
    );

    const rows = screen.getAllByRole('button');
    expect(rows).toHaveLength(2);
    expect(rows[0].className).not.toContain(' active');
    expect(rows[1].className).toContain(' active');
  });

  it('does not highlight activation and deactivation records together', () => {
    const sharedAlertId = 'f1:aerpaw:warn';
    const alerts: Alert[] = [
      {
        alert_id: sharedAlertId,
        flight_id: 'f1',
        active: true,
        activated_at: 1700000000,
        deactivated_at: null,
      },
      {
        alert_id: sharedAlertId,
        flight_id: 'f1',
        active: false,
        activated_at: 1700000000,
        deactivated_at: 1700000300,
      },
    ];
    const selectedKey = alertEpisodeKey(alerts[0]);

    render(
      <ul>
        {alerts.map((alert, index) => {
          const episodeKey = alertEpisodeKey(alert, index);
          return (
            <AlertListItem
              key={`${episodeKey}:${index}`}
              episodeKey={episodeKey}
              alert={alert}
              active={episodeKey === selectedKey}
              sortField="activated"
              onSelect={vi.fn()}
            />
          );
        })}
      </ul>,
    );

    const rows = screen.getAllByRole('button');
    expect(rows[0].className).toContain(' active');
    expect(rows[1].className).not.toContain(' active');
  });

  it('omits active alerts count badge (red dot) on Alerts tab in all views', () => {
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
      alertSortField: 'activated' as const,
      alertSortDirection: 'desc' as const,
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

    const { rerender } = render(<Sidebar {...sidebarProps} portalView="live" />);
    expect(document.querySelector('.alerts-badge-count')).toBeNull();

    rerender(<Sidebar {...sidebarProps} portalView="history" />);
    expect(document.querySelector('.alerts-badge-count')).toBeNull();
  });
});
