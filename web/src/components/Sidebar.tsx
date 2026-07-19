import { useEffect } from 'react';
import type { Alert, FlightSummary } from '../api/types';
import { AlertLevelBadge, flightTimeLabel, LevelBadge } from './LevelBadge';

type SidebarTab = 'flights' | 'alerts';

interface SidebarProps {
  portalView: 'live' | 'history';
  sidebarTab: SidebarTab;
  searchQuery: string;
  flights: FlightSummary[];
  alerts: Alert[];
  activeFlightId: string | null;
  activeAlertId: string | null;
  flightCount: number;
  unreadAlertsCount?: number;
  onSwitchPortalView: (view: 'live' | 'history') => void;
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
  activeFlightId,
  activeAlertId,
  flightCount,
  unreadAlertsCount = 0,
  onSwitchPortalView,
  onSwitchSidebarTab,
  onSearchChange,
  onSelectFlight,
  onSelectAlert,
  onAlertsScroll,
}: SidebarProps) {
  useEffect(() => {
    if (activeAlertId && sidebarTab === 'alerts') {
      const el = document.querySelector('#alert-list li.active');
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    }
  }, [activeAlertId, sidebarTab]);
  return (
    <div id="sidebar">
      <div id="sidebar-header">
        <h1>PyAerial Live Tracker</h1>
        <p>See the data captured by your ADS-B receiver</p>
      </div>
      <div id="view-toggle">
        <button
          type="button"
          className={`view-btn${portalView === 'live' ? ' active' : ''}`}
          id="view-live"
          onClick={() => onSwitchPortalView('live')}
        >
          Live
        </button>
        <button
          type="button"
          className={`view-btn${portalView === 'history' ? ' active' : ''}`}
          id="view-history"
          onClick={() => onSwitchPortalView('history')}
        >
          Historical
        </button>
      </div>
      <div id="search-container">
        <input
          type="text"
          id="search-input"
          placeholder="Search by callsign, ICAO, model, or zone..."
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
        />
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
      <div id="sidebar-tabs">
        <button
          type="button"
          className={`sidebar-tab${sidebarTab === 'flights' ? ' active' : ''}`}
          id="tab-flights"
          onClick={() => onSwitchSidebarTab('flights')}
        >
          Flights
        </button>
        <button
          type="button"
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
      <div id="panel-flights" className={`sidebar-panel${sidebarTab === 'flights' ? ' active' : ''}`}>
        <ul id="flight-list">
          {flights.map((flight) => (
            <li
              key={flight.flight_id}
              className={`flight-item${flight.flight_id === activeFlightId ? ' active' : ''}`}
              onClick={() => onSelectFlight(flight.flight_id)}
            >
              <div className="flight-meta-row">
                <span className="flight-callsign">
                  {flight.callsign || 'UNKNOWN'}{' '}
                  <LevelBadge flight={flight} />
                </span>
                <span className="flight-icao">{flight.icao.toUpperCase()}</span>
              </div>
              <div className="flight-meta-row">
                <span className="flight-desc">{flight.model || 'Unknown Model'}</span>
                <span className="flight-time">{flightTimeLabel(flight)}</span>
              </div>
            </li>
          ))}
        </ul>
      </div>
      <div
        id="panel-alerts"
        className={`sidebar-panel${sidebarTab === 'alerts' ? ' active' : ''}`}
        onScroll={(e) => onAlertsScroll(e.currentTarget)}
      >
        <ul id="alert-list">
          {alerts.map((alert) => {
            const level = (alert.level || 'event').toLowerCase();
            const timeStr = alert.timestamp
              ? new Date(alert.timestamp * 1000).toLocaleTimeString([], {
                  hour: '2-digit',
                  minute: '2-digit',
                  second: '2-digit',
                })
              : '';
            const latVal = alert.latitude != null ? alert.latitude.toFixed(5) : 'N/A';
            const lonVal = alert.longitude != null ? alert.longitude.toFixed(5) : 'N/A';
            const title = `Triggered:\nTime: ${alert.timestamp ? new Date(alert.timestamp * 1000).toLocaleString() : 'N/A'}\nPosition: ${latVal}, ${lonVal}`;
            return (
              <li
                key={alert.alert_id}
                className={`alert-item ${level}${alert.alert_id === activeAlertId ? ' active' : ''}`}
                title={title}
                onClick={() => onSelectAlert(alert)}
              >
                <div className="flight-meta-row">
                  <span className="flight-callsign">
                    {alert.callsign || 'UNKNOWN'}{' '}
                    <AlertLevelBadge level={alert.level} />
                  </span>
                  <span className="flight-icao">{(alert.icao || '').toUpperCase()}</span>
                </div>
                <div className="flight-meta-row">
                  <span className="flight-desc">{alert.zone || 'Zone'}</span>
                  <span className="flight-time">{timeStr}</span>
                </div>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

export type { SidebarTab };
