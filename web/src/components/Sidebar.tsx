import { useEffect, useMemo } from 'react';
import type { Alert, FlightSummary, ServerStats, Zone } from '../api/types';
import { type FlightSortField, type SortDirection } from '../utils/flightData';
import {
  alertEpisodeKey,
  sortAlertsBy,
  type AlertSortField,
  type SortDirection as AlertSortDirection,
} from '../utils/alertData';
import { isAlertActive } from '../utils/format';
import { AlertListItem } from './AlertListItem';
import { AlertSortControls } from './AlertSortControls';
import { FlightListItem } from './FlightListItem';
import { FlightSortControls } from './FlightSortControls';
import { StatusMessage } from './StatusMessage';
import { Button, Input, Stat, StatValue, Tab, TabList } from './ui';

type SidebarTab = 'flights' | 'alerts';

interface SidebarProps {
  portalView: 'live' | 'history';
  sidebarTab: SidebarTab;
  searchQuery: string;
  flights: FlightSummary[];
  alerts: Alert[];
  allAlerts: Alert[];
  unreadAlertsCount?: number;
  serverStats?: ServerStats | null;
  activeFlightId: string | null;
  activeAlertId: string | null;
  flightCount: number;
  flightSortField: FlightSortField;
  flightSortDirection: SortDirection;
  alertSortField: AlertSortField;
  alertSortDirection: AlertSortDirection;
  isLoadingFlights?: boolean;
  isLoadingAlerts?: boolean;
  flightsError?: string | null;
  alertsError?: string | null;
  onRetryFlights?: () => void;
  onRetryAlerts?: () => void;
  onSwitchPortalView: (view: 'live' | 'history') => void;
  onFlightSortChange: (field: FlightSortField) => void;
  onFlightSortDirectionToggle: () => void;
  onAlertSortChange: (field: AlertSortField) => void;
  onAlertSortDirectionToggle: () => void;
  onSwitchSidebarTab: (tab: SidebarTab) => void;
  onSearchChange: (query: string) => void;
  historySinceDate?: string;
  historyUntilDate?: string;
  onHistorySinceChange?: (value: string) => void;
  onHistoryUntilChange?: (value: string) => void;
  onSelectFlight: (flightId: string) => void;
  onSelectAlert: (alert: Alert, episodeKey: string) => void;
  onAlertsScroll: (el: HTMLElement) => void;
  onFlightsScroll?: (el: HTMLElement) => void;
  zones?: Zone[];
  alertColors?: Record<string, string>;
}

export function Sidebar({
  portalView,
  sidebarTab,
  searchQuery,
  flights,
  alerts,
  allAlerts,
  unreadAlertsCount = 0,
  serverStats,
  activeFlightId,
  activeAlertId,
  flightCount,
  flightSortField,
  flightSortDirection,
  alertSortField,
  alertSortDirection,
  isLoadingFlights = false,
  isLoadingAlerts = false,
  flightsError = null,
  alertsError = null,
  onRetryFlights,
  onRetryAlerts,
  onSwitchPortalView,
  onFlightSortChange,
  onFlightSortDirectionToggle,
  onAlertSortChange,
  onAlertSortDirectionToggle,
  onSwitchSidebarTab,
  onSearchChange,
  historySinceDate = '',
  historyUntilDate = '',
  onHistorySinceChange,
  onHistoryUntilChange,
  onSelectFlight,
  onSelectAlert,
  onAlertsScroll,
  onFlightsScroll,
  zones,
  alertColors,
}: SidebarProps) {
  useEffect(() => {
    if (activeAlertId && sidebarTab === 'alerts') {
      document.querySelector('#alert-list .ui-row.is-active')?.scrollIntoView({
        behavior: 'smooth',
        block: 'nearest',
      });
    }
  }, [activeAlertId, sidebarTab]);

  const activeAlertsCount = useMemo(() => {
    return allAlerts.filter(isAlertActive).length;
  }, [allAlerts]);

  const displayFlightCount = useMemo(() => {
    if (portalView === 'live') {
      return serverStats?.live_flights ?? flightCount;
    }
    return serverStats?.retained_flights ?? flightCount;
  }, [serverStats, portalView, flightCount]);

  const displayAlertCount = useMemo(() => {
    if (portalView === 'live') {
      return serverStats?.active_alerts ?? activeAlertsCount;
    }
    return serverStats?.historical_alerts ?? allAlerts.length;
  }, [serverStats, portalView, activeAlertsCount, allAlerts.length]);

  const alertCountByFlight = useMemo(() => {
    const map = new Map<string, number>();
    for (const alert of allAlerts) {
      if (alert.flight_id) {
        map.set(alert.flight_id, (map.get(alert.flight_id) ?? 0) + 1);
      }
    }
    return map;
  }, [allAlerts]);

  const sortedAlerts = useMemo(
    () => sortAlertsBy(alerts, alertSortField, alertSortDirection),
    [alerts, alertSortField, alertSortDirection],
  );

  return (
    <div id="sidebar">
      <div id="sidebar-header">
        <div className="sidebar-header-top">
          <div className="sidebar-header-text">
            <div className="sidebar-title-row">
              <a
                href="https://github.com/quantumbagel/PyAerial"
                target="_blank"
                rel="noreferrer"
                aria-label="GitHub Repository"
                title="GitHub Repository"
                className="github-link"
              >
                <svg height="18" width="18" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                  <path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z" />
                </svg>
              </a>
              <h1>PyAerial Live Tracker</h1>
            </div>
          </div>
          <div className="sidebar-header-controls">
            <div id="view-toggle" className="ui-btn-group" role="group" aria-label="Portal view">
              <Button
                variant="toggle"
                flex
                active={portalView === 'live'}
                id="view-live"
                aria-pressed={portalView === 'live'}
                onClick={() => onSwitchPortalView('live')}
              >
                Live
              </Button>
              <Button
                variant="toggle"
                flex
                active={portalView === 'history'}
                id="view-history"
                aria-pressed={portalView === 'history'}
                onClick={() => onSwitchPortalView('history')}
              >
                Historical
              </Button>
            </div>
            <div id="stats-panel" className="ui-btn-group">
              <Stat>
                <span id="flight-stat-label">{portalView === 'live' ? 'Live:' : 'Retained:'}</span>
                <StatValue id="flight-count" tone="live">
                  {displayFlightCount}
                </StatValue>
              </Stat>
              <Stat title={portalView === 'live' ? `${displayAlertCount} active alert episode(s)` : `${displayAlertCount} total alert(s)`}>
                <span>{portalView === 'live' ? 'Active Alerts:' : 'Alerts:'}</span>
                <StatValue
                  id="alert-count"
                  tone="warn"
                >
                  {displayAlertCount}
                </StatValue>
              </Stat>
            </div>
          </div>
        </div>
      </div>
      <div id="search-container">
        <Input
          type="search"
          id="search-input"
          placeholder={
            portalView === 'history'
              ? 'Search callsign, ICAO, or flight id…'
              : 'Search by callsign, ICAO, model, or alert…'
          }
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
          aria-label="Search flights and alerts"
        />
        {portalView === 'history' ? (
          <div id="history-filters">
            <label className="history-filter">
              <span>From</span>
              <Input
                type="date"
                value={historySinceDate}
                max={historyUntilDate || undefined}
                onChange={(e) => onHistorySinceChange?.(e.target.value)}
                aria-label="History from date"
              />
            </label>
            <label className="history-filter">
              <span>To</span>
              <Input
                type="date"
                value={historyUntilDate}
                min={historySinceDate || undefined}
                onChange={(e) => onHistoryUntilChange?.(e.target.value)}
                aria-label="History to date"
              />
            </label>
          </div>
        ) : null}
      </div>
      <TabList compact id="sidebar-tabs" aria-label="Sidebar panels">
        <Tab
          id="tab-flights"
          active={sidebarTab === 'flights'}
          onClick={() => onSwitchSidebarTab('flights')}
        >
          Flights
        </Tab>
        <Tab
          id="tab-alerts"
          active={sidebarTab === 'alerts'}
          onClick={() => onSwitchSidebarTab('alerts')}
        >
          Alerts
          {unreadAlertsCount > 0 && sidebarTab !== 'alerts' ? (
            <span className="ui-count" aria-label={`${unreadAlertsCount} unread alerts`}>
              {unreadAlertsCount}
            </span>
          ) : null}
        </Tab>
      </TabList>
      <div
        id="panel-flights"
        role="tabpanel"
        className={`sidebar-panel${sidebarTab === 'flights' ? ' active' : ''}`}
      >
        <FlightSortControls
          flightSortField={flightSortField}
          flightSortDirection={flightSortDirection}
          onFlightSortChange={onFlightSortChange}
          onFlightSortDirectionToggle={onFlightSortDirectionToggle}
        />
        <ul
          id="flight-list"
          onScroll={(e) => onFlightsScroll?.(e.currentTarget)}
        >
          {isLoadingFlights ? (
            <StatusMessage variant="loading">Loading flights…</StatusMessage>
          ) : flightsError ? (
            <StatusMessage variant="error" onRetry={onRetryFlights}>
              {flightsError}
            </StatusMessage>
          ) : flights.length === 0 ? (
            <StatusMessage>
              {searchQuery || historySinceDate || historyUntilDate
                ? 'No flights match your filters.'
                : 'No flights available.'}
            </StatusMessage>
          ) : (
            flights.map((flight) => (
              <FlightListItem
                key={flight.flight_id}
                flight={flight}
                active={flight.flight_id === activeFlightId}
                sortField={flightSortField}
                alertCount={alertCountByFlight.get(flight.flight_id)}
                zones={zones}
                alertColors={alertColors}
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
      >
        <AlertSortControls
          alertSortField={alertSortField}
          alertSortDirection={alertSortDirection}
          onAlertSortChange={onAlertSortChange}
          onAlertSortDirectionToggle={onAlertSortDirectionToggle}
        />
        <ul
          id="alert-list"
          key={`${alertSortField}-${alertSortDirection}`}
          onScroll={(e) => onAlertsScroll(e.currentTarget)}
        >
          {isLoadingAlerts ? (
            <StatusMessage variant="loading">Loading alerts…</StatusMessage>
          ) : alertsError ? (
            <StatusMessage variant="error" onRetry={onRetryAlerts}>
              {alertsError}
            </StatusMessage>
          ) : sortedAlerts.length === 0 ? (
            <StatusMessage>
              {searchQuery || historySinceDate || historyUntilDate
                ? 'No alerts match your filters.'
                : 'No alerts yet.'}
            </StatusMessage>
          ) : (
            sortedAlerts.map((alert, index) => {
              const episodeKey = alertEpisodeKey(alert, index);
              return (
              <AlertListItem
                key={`${episodeKey}:${index}`}
                episodeKey={episodeKey}
                alert={alert}
                active={episodeKey === activeAlertId}
                sortField={alertSortField}
                zones={zones}
                alertColors={alertColors}
                onSelect={onSelectAlert}
              />
              );
            })
          )}
        </ul>
      </div>
    </div>
  );
}

export type { SidebarTab };
