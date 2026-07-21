import { useEffect, useMemo } from 'react';
import type { Alert, FlightSummary } from '../api/types';
import { FLIGHT_SORT_OPTIONS, type FlightSortField, type SortDirection } from '../utils/flightData';
import { AlertListItem } from './AlertListItem';
import { FlightListItem } from './FlightListItem';
import { StatusMessage } from './StatusMessage';

type SidebarTab = 'flights' | 'alerts';

interface SidebarProps {
  portalView: 'live' | 'history';
  sidebarTab: SidebarTab;
  searchQuery: string;
  flights: FlightSummary[];
  alerts: Alert[];
  allAlerts: Alert[];
  activeFlightId: string | null;
  activeAlertId: string | null;
  flightCount: number;
  flightSortField: FlightSortField;
  flightSortDirection: SortDirection;
  unreadAlertsCount?: number;
  isLoadingFlights?: boolean;
  isLoadingAlerts?: boolean;
  flightsError?: string | null;
  alertsError?: string | null;
  notificationsEnabled: boolean;
  onRetryFlights?: () => void;
  onRetryAlerts?: () => void;
  onEnableNotifications: () => void;
  onSwitchPortalView: (view: 'live' | 'history') => void;
  onFlightSortChange: (field: FlightSortField) => void;
  onFlightSortDirectionToggle: () => void;
  onSwitchSidebarTab: (tab: SidebarTab) => void;
  onSearchChange: (query: string) => void;
  onSelectFlight: (flightId: string) => void;
  onSelectAlert: (alert: Alert) => void;
  onAlertsScroll: (el: HTMLDivElement) => void;
}

export function Sidebar({
  portalView,
  sidebarTab,
  searchQuery,
  flights,
  alerts,
  allAlerts,
  activeFlightId,
  activeAlertId,
  flightCount,
  flightSortField,
  flightSortDirection,
  unreadAlertsCount = 0,
  isLoadingFlights = false,
  isLoadingAlerts = false,
  flightsError = null,
  alertsError = null,
  notificationsEnabled,
  onRetryFlights,
  onRetryAlerts,
  onEnableNotifications,
  onSwitchPortalView,
  onFlightSortChange,
  onFlightSortDirectionToggle,
  onSwitchSidebarTab,
  onSearchChange,
  onSelectFlight,
  onSelectAlert,
  onAlertsScroll,
}: SidebarProps) {
  useEffect(() => {
    if (activeAlertId && sidebarTab === 'alerts') {
      document.querySelector('#alert-list .alert-item.active')?.scrollIntoView({
        behavior: 'smooth',
        block: 'nearest',
      });
    }
  }, [activeAlertId, sidebarTab]);

  const alertCountByFlight = useMemo(() => {
    const map = new Map<string, number>();
    for (const alert of allAlerts) {
      if (alert.flight_id) {
        map.set(alert.flight_id, (map.get(alert.flight_id) ?? 0) + 1);
      }
    }
    return map;
  }, [allAlerts]);

  const showNotificationPrompt =
    typeof Notification !== 'undefined' &&
    Notification.permission === 'default' &&
    !notificationsEnabled;

  return (
    <div id="sidebar">
      <div id="sidebar-header">
        <div className="sidebar-header-top">
          <div className="sidebar-header-text">
            <h1>PyAerial Live Tracker</h1>
            <p>See the data captured by your ADS-B receiver</p>
          </div>
          <div className="sidebar-header-controls">
            <div id="view-toggle" role="group" aria-label="Portal view">
              <button
                type="button"
                className={`view-btn${portalView === 'live' ? ' active' : ''}`}
                id="view-live"
                aria-pressed={portalView === 'live'}
                onClick={() => onSwitchPortalView('live')}
              >
                Live
              </button>
              <button
                type="button"
                className={`view-btn${portalView === 'history' ? ' active' : ''}`}
                id="view-history"
                aria-pressed={portalView === 'history'}
                onClick={() => onSwitchPortalView('history')}
              >
                Historical
              </button>
            </div>
            <div id="stats-panel">
              <div className="stat-card">
                <span id="flight-stat-label">{portalView === 'live' ? 'Live:' : 'Retained:'}</span>
                <strong id="flight-count" className="stat-live">
                  {flightCount}
                </strong>
              </div>
              <div className="stat-card">
                <span>Alerts:</span>
                <strong id="alert-count" className="stat-alerts">
                  {alerts.length}
                </strong>
              </div>
            </div>
          </div>
        </div>
        {showNotificationPrompt && (
          <div className="notification-prompt">
            <span>Enable desktop alerts for new events</span>
            <button type="button" onClick={onEnableNotifications}>
              Enable
            </button>
          </div>
        )}
      </div>
      <div id="search-container">
        <input
          type="search"
          id="search-input"
          placeholder="Search by callsign, ICAO, model, or zone..."
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
          aria-label="Search flights and alerts"
        />
      </div>
      <div id="sidebar-tabs" role="tablist" aria-label="Sidebar panels">
        <button
          type="button"
          role="tab"
          aria-selected={sidebarTab === 'flights'}
          className={`sidebar-tab${sidebarTab === 'flights' ? ' active' : ''}`}
          id="tab-flights"
          onClick={() => onSwitchSidebarTab('flights')}
        >
          Flights
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={sidebarTab === 'alerts'}
          className={`sidebar-tab${sidebarTab === 'alerts' ? ' active' : ''}`}
          id="tab-alerts"
          onClick={() => onSwitchSidebarTab('alerts')}
        >
          Alerts
          {unreadAlertsCount > 0 && (
            <span className="alerts-badge-count">{unreadAlertsCount}</span>
          )}
        </button>
      </div>
      <div
        id="panel-flights"
        role="tabpanel"
        className={`sidebar-panel${sidebarTab === 'flights' ? ' active' : ''}`}
      >
        <div id="flight-sort-bar" className="flight-sort-bar">
          <label htmlFor="flight-sort-field" className="flight-sort-label">
            Sort by
          </label>
          <select
            id="flight-sort-field"
            className="flight-sort-select"
            value={flightSortField}
            onChange={(e) => onFlightSortChange(e.target.value as FlightSortField)}
          >
            {FLIGHT_SORT_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="flight-sort-direction"
            onClick={onFlightSortDirectionToggle}
            title={flightSortDirection === 'asc' ? 'Ascending' : 'Descending'}
            aria-label={flightSortDirection === 'asc' ? 'Sort ascending' : 'Sort descending'}
          >
            {flightSortDirection === 'asc' ? '↑' : '↓'}
          </button>
        </div>
        <ul id="flight-list">
          {isLoadingFlights ? (
            <StatusMessage variant="loading">Loading flights…</StatusMessage>
          ) : flightsError ? (
            <StatusMessage variant="error" onRetry={onRetryFlights}>
              {flightsError}
            </StatusMessage>
          ) : flights.length === 0 ? (
            <StatusMessage>
              {searchQuery ? 'No flights match your search.' : 'No flights available.'}
            </StatusMessage>
          ) : (
            flights.map((flight) => (
              <FlightListItem
                key={flight.flight_id}
                flight={flight}
                active={flight.flight_id === activeFlightId}
                sortField={flightSortField}
                alertCount={alertCountByFlight.get(flight.flight_id)}
                onSelect={onSelectFlight}
              />
            ))
          )}
        </ul>
      </div>
      <div
        id="panel-alerts"
        role="tabpanel"
        className={`sidebar-panel${sidebarTab === 'alerts' ? ' active' : ''}`}
        onScroll={(e) => onAlertsScroll(e.currentTarget)}
      >
        <ul id="alert-list">
          {isLoadingAlerts ? (
            <StatusMessage variant="loading">Loading alerts…</StatusMessage>
          ) : alertsError ? (
            <StatusMessage variant="error" onRetry={onRetryAlerts}>
              {alertsError}
            </StatusMessage>
          ) : alerts.length === 0 ? (
            <StatusMessage>
              {searchQuery ? 'No alerts match your search.' : 'No alerts yet'}
            </StatusMessage>
          ) : (
            alerts.map((alert) => (
              <AlertListItem
                key={alert.alert_id}
                alert={alert}
                active={alert.alert_id === activeAlertId}
                onSelect={onSelectAlert}
              />
            ))
          )}
        </ul>
      </div>
    </div>
  );
}

export type { SidebarTab };
