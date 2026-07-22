import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DetailsDrawer } from './DetailsDrawer';
import type { FlightDetail } from '../api/types';

describe('DetailsDrawer loading state', () => {
  const defaultProps = {
    open: true,
    flightDetail: null,
    activeAlertId: null,
    flightAlerts: [],
    flightTelemetry: [],
    drawerTab: 'telemetry' as const,
    selectedTelemetryPoint: null,
    appConfig: null,
    onSelectTelemetryPoint: vi.fn(),
    onClose: vi.fn(),
    onSwitchTab: vi.fn(),
    onSelectAlert: vi.fn(),
  };

  it('renders loading plane details with spinner when loading', () => {
    render(<DetailsDrawer {...defaultProps} isLoading={true} />);

    expect(screen.queryByText('UNKNOWN')).toBeNull();
    const loadingTexts = screen.getAllByText('Loading plane details…');
    expect(loadingTexts.length).toBeGreaterThan(0);
    const spinner = document.querySelector('.flight-list-spinner');
    expect(spinner).not.toBeNull();
  });

  it('renders loading state when flightDetail is null and selectionError is null', () => {
    render(<DetailsDrawer {...defaultProps} flightDetail={null} selectionError={null} />);

    expect(screen.queryByText('UNKNOWN')).toBeNull();
    const loadingTexts = screen.getAllByText('Loading plane details…');
    expect(loadingTexts.length).toBeGreaterThan(0);
  });

  it('renders flight details when loaded', () => {
    const flightDetail: FlightDetail = {
      flight_id: '123',
      icao: 'a1b2c3',
      callsign: 'AAL456',
      registration: 'N123AA',
      model: 'Boeing 737-800',
      aircraft_type: 'B738',
      owner: 'American Airlines',
      country: 'United States',
      is_live: true,
      timestamp: 1700000000,
    };

    render(<DetailsDrawer {...defaultProps} flightDetail={flightDetail} isLoading={false} />);

    expect(screen.getByText('AAL456')).not.toBeNull();
    expect(screen.getByText('A1B2C3')).not.toBeNull();
    expect(screen.getByText('Boeing 737-800')).not.toBeNull();
  });

  it('never renders UNKNOWN when flightDetail callsign is UNKNOWN or missing', () => {
    const flightDetail: FlightDetail = {
      flight_id: '123',
      icao: 'A1B2C3',
      callsign: 'UNKNOWN',
      registration: 'N123AA',
      model: 'Cessna 172',
      is_live: true,
    };

    render(<DetailsDrawer {...defaultProps} flightDetail={flightDetail} isLoading={false} />);

    expect(screen.queryByText('UNKNOWN')).toBeNull();
    expect(screen.getAllByText('A1B2C3').length).toBeGreaterThan(0);
  });

  it('renders both First Seen and Last Seen for live flights', () => {
    const flightDetail: FlightDetail = {
      flight_id: '123',
      icao: 'A1B2C3',
      callsign: 'LIVE123',
      is_live: true,
      start_time: 1700000000,
      timestamp: 1700000300,
    };

    render(<DetailsDrawer {...defaultProps} flightDetail={flightDetail} isLoading={false} />);

    expect(screen.getByText('First Seen')).not.toBeNull();
    expect(document.querySelector('#detail-first-seen')).not.toBeNull();
    expect(screen.getByText('Last Seen')).not.toBeNull();
    expect(document.querySelector('#detail-last-seen')).not.toBeNull();
  });

  it('uses latest telemetry point timestamp for Last Seen on live flight', () => {
    const flightDetail: FlightDetail = {
      flight_id: '123',
      icao: 'A1B2C3',
      callsign: 'LIVE123',
      is_live: true,
      start_time: 1700000000,
      timestamp: 1700000100, // Older timestamp in flightDetail
    };
    const flightTelemetry = [
      { timestamp: 1700000100, latitude: 10, longitude: 20 },
      { timestamp: 1700000500, latitude: 11, longitude: 21 }, // Newer telemetry point
    ];

    render(
      <DetailsDrawer
        {...defaultProps}
        flightDetail={flightDetail}
        flightTelemetry={flightTelemetry}
        isLoading={false}
      />,
    );

    const lastSeenEl = document.querySelector('#detail-last-seen');
    expect(lastSeenEl).not.toBeNull();
    // The rendered time should correspond to 1700000500 rather than 1700000100
    const formattedNewerTime = new Date(1700000500 * 1000).toLocaleTimeString([], {
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
    });
    expect(lastSeenEl?.textContent).toContain(formattedNewerTime);
  });
});
