import { useEffect, useMemo, useRef } from 'react';
import type { Alert, FlightSummary, PortalView, RawAntenna, ServerStats, Zone } from '../api/types';
import {
  ALERT_SORT_OPTIONS,
  alertEpisodeKey,
  isAlertActive,
  sortAlertsBy,
  type AlertSortField,
  type SortDirection,
} from '../utils/alertData';
import { emptyAlertsMessage, emptyFlightsMessage } from '../utils/emptyStates';
import { FLIGHT_SORT_OPTIONS, type FlightSortField } from '../utils/flightData';
import { formatNumber } from '../utils/format';
import { AlertListItem } from './AlertListItem';
import { FlightListItem } from './FlightListItem';
import { RawPanel } from './RawPanel';
import {
  Button,
  Input,
  SortControl,
  Stat,
  StatValue,
  StateView,
  Tab,
  TabList,
  TabPanel,
  useTabIds,
} from './ui';

type SidebarTab = 'flights' | 'alerts' | 'raw';

export const VIEW_LABEL: Record<PortalView, string> = { live: 'Live', history: 'History' };

function nearBottom(el: HTMLElement): boolean {
  return el.scrollTop + el.clientHeight >= el.scrollHeight - 50;
}

interface SidebarProps {
  portalView: PortalView;
  sidebarTab: SidebarTab;
  searchQuery: string;
  flights: FlightSummary[];
  alerts: Alert[];
  allAlerts: Alert[];
  allFlights: FlightSummary[];
  unreadAlertsCount?: number;
  serverStats?: ServerStats | null;
  activeFlightId: string | null;
  activeAlertId: string | null;
  flightCount: number;
  flightSortField: FlightSortField;
  flightSortDirection: SortDirection;
  alertSortField: AlertSortField;
  alertSortDirection: SortDirection;
  isLoadingFlights?: boolean;
  isLoadingAlerts?: boolean;
  flightsError?: string | null;
  alertsError?: string | null;
  onRetryFlights?: () => void;
  onRetryAlerts?: () => void;
  onSwitchPortalView: (view: PortalView) => void;
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
  onLoadMoreAlerts?: () => void;
  onLoadMoreFlights?: () => void;
  zones?: Zone[];
  alertColors?: Record<string, string>;
  rawAntenna?: RawAntenna | null;
  onSelectRawIcao?: (icao: string) => void;
  /** Small screens: the sidebar is a slide-in panel. */
  open?: boolean;
  id?: string;
}

export function Sidebar({
  portalView,
  sidebarTab,
  searchQuery,
  flights,
  alerts,
  allAlerts,
  allFlights,
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
  onLoadMoreAlerts,
  onLoadMoreFlights,
  zones,
  alertColors,
  rawAntenna = null,
  onSelectRawIcao,
  open = false,
  id,
}: SidebarProps) {
  const tabIds = useTabIds();
  const alertListRef = useRef<HTMLUListElement>(null);

  useEffect(() => {
    if (activeAlertId && sidebarTab === 'alerts') {
      alertListRef.current
        ?.querySelector('.ui-row.is-active')
        ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [activeAlertId, sidebarTab]);

  const activeAlertsCount = useMemo(() => allAlerts.filter(isAlertActive).length, [allAlerts]);
  const hasFilters = Boolean(searchQuery.trim() || historySinceDate || historyUntilDate);
  const isLive = portalView === 'live';

  const displayFlightCount = isLive
    ? (serverStats?.live_flights ?? flightCount)
    : !hasFilters
      ? (serverStats?.retained_flights ?? flightCount)
      : flightCount;
  const displayAlertCount = isLive
    ? (serverStats?.active_alerts ?? activeAlertsCount)
    : !hasFilters
      ? (serverStats?.historical_alerts ?? alerts.length)
      : alerts.length;

  const sortedAlerts = useMemo(
    () => sortAlertsBy(alerts, alertSortField, alertSortDirection),
    [alerts, alertSortField, alertSortDirection],
  );

  return (
    <nav id={id} className={`sidebar${open ? ' is-open' : ''}`} aria-label="Flights and alerts">
      <div className="sidebar-header">
        <div className="sidebar-header-top">
          <div className="sidebar-title-row">
            <a
              href="https://github.com/quantumbagel/PyAerial"
              target="_blank"
              rel="noreferrer"
              aria-label="PyAerial on GitHub"
              title="PyAerial on GitHub"
              className="github-link"
            >
              <svg height="18" width="18" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                <path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z" />
              </svg>
            </a>
            <h1>PyAerial</h1>
          </div>
          <div className="sidebar-header-controls">
            <div className="ui-btn-group" role="group" aria-label="Data source">
              {(['live', 'history'] as const).map((view) => (
                <Button
                  key={view}
                  variant="toggle"
                  flex
                  active={portalView === view}
                  aria-pressed={portalView === view}
                  onClick={() => onSwitchPortalView(view)}
                >
                  {VIEW_LABEL[view]}
                </Button>
              ))}
            </div>
            <div className="sidebar-stats ui-btn-group">
              <Stat
                title={
                  isLive
                    ? 'Aircraft the tracker is following right now'
                    : 'Completed flights kept because a retain rule held long enough'
                }
              >
                <span>{isLive ? 'Aircraft' : 'Flights'}</span>
                <StatValue tone="live">{formatNumber(displayFlightCount)}</StatValue>
              </Stat>
              <Stat title={isLive ? 'Alert episodes active right now' : 'Archived alert episodes'}>
                <span>{isLive ? 'Active alerts' : 'Alerts'}</span>
                <StatValue tone="warn">{formatNumber(displayAlertCount)}</StatValue>
              </Stat>
            </div>
          </div>
        </div>
      </div>
      <div className="sidebar-search">
        <Input
          type="search"
          placeholder={
            sidebarTab === 'raw'
              ? 'Search hex or ICAO…'
              : isLive
                ? 'Search callsign, ICAO, model, or alert…'
                : 'Search callsign, ICAO, or flight ID…'
          }
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
          aria-label={sidebarTab === 'raw' ? 'Search raw frames' : 'Search flights and alerts'}
        />
        {!isLive && sidebarTab !== 'raw' ? (
          <div className="history-filters">
            <label className="history-filter">
              <span>From</span>
              <Input
                type="date"
                value={historySinceDate}
                max={historyUntilDate || undefined}
                onChange={(e) => onHistorySinceChange?.(e.target.value)}
              />
            </label>
            <label className="history-filter">
              <span>To</span>
              <Input
                type="date"
                value={historyUntilDate}
                min={historySinceDate || undefined}
                onChange={(e) => onHistoryUntilChange?.(e.target.value)}
              />
            </label>
          </div>
        ) : null}
      </div>
      <TabList compact aria-label="Sidebar views">
        <Tab
          id={tabIds.tab('flights')}
          controls={tabIds.panel('flights')}
          active={sidebarTab === 'flights'}
          onClick={() => onSwitchSidebarTab('flights')}
        >
          Flights
        </Tab>
        <Tab
          id={tabIds.tab('alerts')}
          controls={tabIds.panel('alerts')}
          active={sidebarTab === 'alerts'}
          onClick={() => onSwitchSidebarTab('alerts')}
        >
          Alerts
          {unreadAlertsCount > 0 && sidebarTab !== 'alerts' ? (
            <span className="ui-count">
              {unreadAlertsCount}
              <span className="visually-hidden"> new</span>
            </span>
          ) : null}
        </Tab>
        <Tab
          id={tabIds.tab('raw')}
          controls={tabIds.panel('raw')}
          active={sidebarTab === 'raw'}
          onClick={() => onSwitchSidebarTab('raw')}
        >
          Raw
        </Tab>
      </TabList>
      <TabPanel
        id={tabIds.panel('flights')}
        labelledBy={tabIds.tab('flights')}
        active={sidebarTab === 'flights'}
        className="sidebar-panel"
      >
        <SortControl
          noun="flights"
          options={FLIGHT_SORT_OPTIONS}
          value={flightSortField}
          direction={flightSortDirection}
          onChange={onFlightSortChange}
          onToggleDirection={onFlightSortDirectionToggle}
        />
        <ul className="sidebar-list" onScroll={(e) => nearBottom(e.currentTarget) && onLoadMoreFlights?.()}>
          {isLoadingFlights ? (
            <StateView as="li" kind="loading">
              Loading flights…
            </StateView>
          ) : flightsError ? (
            <StateView as="li" kind="error" onRetry={onRetryFlights}>
              {flightsError}
            </StateView>
          ) : flights.length === 0 ? (
            <StateView as="li">
              {emptyFlightsMessage({ view: portalView, hasFilters, stats: serverStats ?? null })}
            </StateView>
          ) : (
            flights.map((flight) => (
              <FlightListItem
                key={flight.flight_id}
                flight={flight}
                active={flight.flight_id === activeFlightId}
                sortField={flightSortField}
                zones={zones}
                alertColors={alertColors}
                onSelect={onSelectFlight}
              />
            ))
          )}
        </ul>
      </TabPanel>
      <TabPanel
        id={tabIds.panel('alerts')}
        labelledBy={tabIds.tab('alerts')}
        active={sidebarTab === 'alerts'}
        className="sidebar-panel"
      >
        <SortControl
          noun="alerts"
          options={ALERT_SORT_OPTIONS}
          value={alertSortField}
          direction={alertSortDirection}
          onChange={onAlertSortChange}
          onToggleDirection={onAlertSortDirectionToggle}
        />
        <ul
          ref={alertListRef}
          className="sidebar-list"
          onScroll={(e) => nearBottom(e.currentTarget) && onLoadMoreAlerts?.()}
        >
          {isLoadingAlerts ? (
            <StateView as="li" kind="loading">
              Loading alerts…
            </StateView>
          ) : alertsError ? (
            <StateView as="li" kind="error" onRetry={onRetryAlerts}>
              {alertsError}
            </StateView>
          ) : sortedAlerts.length === 0 ? (
            <StateView as="li">
              {emptyAlertsMessage({ view: portalView, hasFilters, stats: serverStats ?? null })}
            </StateView>
          ) : (
            sortedAlerts.map((alert) => {
              const episodeKey = alertEpisodeKey(alert);
              return (
                <AlertListItem
                  key={episodeKey}
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
      </TabPanel>
      <TabPanel
        id={tabIds.panel('raw')}
        labelledBy={tabIds.tab('raw')}
        active={sidebarTab === 'raw'}
        className="sidebar-panel"
      >
        {sidebarTab === 'raw' ? (
          <RawPanel
            searchQuery={searchQuery}
            flights={allFlights}
            antenna={rawAntenna}
            stats={serverStats ?? null}
            onSelectIcao={onSelectRawIcao}
          />
        ) : null}
      </TabPanel>
    </nav>
  );
}

export type { SidebarTab };
