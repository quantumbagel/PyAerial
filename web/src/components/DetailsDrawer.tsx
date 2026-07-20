import { useEffect, useRef, useState } from 'react';
import type { Alert, AppConfig, FlightDetail, TelemetryPoint } from '../api/types';
import { isFlightLive } from '../utils/format';
import {
  formatAlertAltitude,
  formatAlertEta,
  formatAlertLevel,
  formatAltitude,
  formatAltitudeCell,
  formatHeading,
  formatSpeed,
  formatSpeedCell,
  formatZoneLevel,
} from '../utils/format';

type DrawerTab = 'alerts' | 'telemetry';

interface DetailsDrawerProps {
  open: boolean;
  flightDetail: FlightDetail | null;
  activeAlertId: string | null;
  flightAlerts: Alert[];
  flightTelemetry: TelemetryPoint[];
  drawerTab: DrawerTab;
  selectedTelemetryPoint: TelemetryPoint | null;
  appConfig: AppConfig | null;
  onClose: () => void;
  onSwitchTab: (tab: DrawerTab) => void;
  onSelectAlert: (alert: Alert) => void;
  onSelectTelemetryPoint: (point: TelemetryPoint) => void;
}

export function DetailsDrawer({
  open,
  flightDetail,
  activeAlertId,
  flightAlerts,
  flightTelemetry,
  drawerTab,
  selectedTelemetryPoint,
  appConfig,
  onClose,
  onSwitchTab,
  onSelectAlert,
  onSelectTelemetryPoint,
}: DetailsDrawerProps) {
  const tableContainerRef = useRef<HTMLDivElement>(null);
  const [prevTelemetryLength, setPrevTelemetryLength] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  // Tick every second when drawer is open and flight is live, to update relative time display
  useEffect(() => {
    if (!open || !flightDetail || !isFlightLive(flightDetail)) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [open, flightDetail]);

  useEffect(() => {
    const el = tableContainerRef.current;
    if (!el) return;

    const diff = flightTelemetry.length - prevTelemetryLength;
    const oldLength = prevTelemetryLength;
    setPrevTelemetryLength(flightTelemetry.length);

    if (oldLength > 0 && diff > 0 && diff < 5) {
      const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (distanceFromBottom < 80) {
        el.scrollTop = el.scrollHeight;
      }
    }
  }, [flightTelemetry, prevTelemetryLength]);

  useEffect(() => {
    if (activeAlertId && drawerTab === 'alerts' && open) {
      const el = document.querySelector('#alert-timeline-list .alert-timeline-item.active');
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    }
  }, [activeAlertId, drawerTab, open]);
  const callsign = flightDetail?.callsign || 'UNKNOWN';
  const icao = flightDetail?.icao?.toUpperCase() || 'N/A';
  const photoUrl =
    typeof flightDetail?.photo_url === 'string'
      ? flightDetail.photo_url
      : null;
  const hasPhoto = Boolean(photoUrl);
  const hasAlert = Boolean(
    (flightDetail?.zone || '').trim() || (flightDetail?.level || '').trim(),
  );

  const sortedAlerts = [...flightAlerts].sort(
    (a, b) => (a.timestamp || 0) - (b.timestamp || 0),
  );

  const lastPoint =
    flightTelemetry.length > 0 ? flightTelemetry[flightTelemetry.length - 1] : null;

  return (
    <div id="details-drawer" className={open ? 'open' : ''}>
      <button type="button" id="close-drawer-btn" className="close-btn" title="Close" onClick={onClose}>
        &times;
      </button>
      <div id="drawer-header">
        <div className="drawer-header-label">Selected Aircraft</div>
        <h2>
          <span id="detail-callsign">{callsign}</span>{' '}
          <span id="detail-icao" className="flight-icao">
            {icao}
          </span>
        </h2>
      </div>
      <div className="drawer-content">
        <div
          id="detail-photo-container"
          style={{ display: hasPhoto ? 'block' : 'none' }}
          className={hasPhoto ? '' : undefined}
        >
          {photoUrl && (
            <img id="detail-photo" src={photoUrl} alt="Aircraft Photo" />
          )}
          <div className="photo-gradient-bottom">
            <span>
              Photo by{' '}
              <span id="detail-photo-photographer" className="photo-credit-name">
                {flightDetail?.photo_photographer || 'Unknown'}
              </span>
            </span>
            <a
              id="detail-photo-link"
              className="photo-link"
              href={flightDetail?.photo_link || '#'}
              target="_blank"
              rel="noreferrer"
            >
              View on Planespotters.net
            </a>
          </div>
        </div>

        <div className="info-section">
          <h3>External Trackers</h3>
          <div className="external-links-grid">
            <a
              href={flightDetail?.callsign ? `https://flightaware.com/live/flight/${flightDetail.callsign.trim()}` : `https://flightaware.com/live/modes/${icao.toLowerCase()}`}
              target="_blank"
              rel="noreferrer"
              className="external-link-btn"
            >
              ✈️ FlightAware
            </a>
            <a
              href={`https://globe.adsbexchange.com/?icao=${icao.toLowerCase()}`}
              target="_blank"
              rel="noreferrer"
              className="external-link-btn"
            >
              🌐 ADS-B Exch
            </a>
            <a
              href={flightDetail?.registration ? `https://www.radarbox.com/data/registration/${flightDetail.registration.trim()}` : `https://www.radarbox.com/data/mode-s/${icao.toLowerCase()}`}
              target="_blank"
              rel="noreferrer"
              className="external-link-btn"
            >
              📦 RadarBox
            </a>
          </div>
        </div>

        <div className="info-section">
          <h3>Aircraft Details</h3>
          <div className="details-grid">
            <span className="details-label">Registration</span>
            <span className="details-value details-value--accent" id="detail-registration">
              {flightDetail?.registration || 'Unknown'}
            </span>
            <span className="details-label">Model</span>
            <span className="details-value" id="detail-model">
              {flightDetail?.model || 'Unknown Model'}
            </span>
            <span className="details-label">Aircraft Type</span>
            <span className="details-value" id="detail-type">
              {flightDetail?.aircraft_type || flightDetail?.typecode || 'Unknown Type'}
            </span>
            <span className="details-label">Owner</span>
            <span className="details-value" id="detail-owner">
              {flightDetail?.owner || 'Unknown Owner'}
            </span>
            <span className="details-label">Registration Country</span>
            <span className="details-value" id="detail-country">
              {flightDetail?.country || 'Unknown'}
            </span>
            <span className="details-label">Zone / Level</span>
            <span
              className={`details-value${hasAlert ? ' details-value--warn' : ' details-value--muted'}`}
              id="detail-zone-level"
            >
              {formatZoneLevel(flightDetail?.zone, flightDetail?.level)}
            </span>
          </div>
        </div>

        <div className="info-section">
          <h3>Telemetry Readings</h3>
          <div className="details-grid">
            {(() => {
              const formatTs = (ts?: number | null) =>
                ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' }) : 'N/A';
              const live = flightDetail ? isFlightLive(flightDetail) : false;
              if (live) {
                const liveTs = flightDetail?.timestamp ?? flightDetail?.end_time ?? flightDetail?.start_time;
                const secsAgo = liveTs ? Math.max(0, Math.round((now / 1000) - liveTs)) : null;
                const rememberSecs = appConfig?.remember_planes ?? null;
                const dropIn = (secsAgo != null && rememberSecs != null)
                  ? Math.max(0, rememberSecs - secsAgo)
                  : null;
                const isWarning = secsAgo != null && secsAgo > (rememberSecs ?? Infinity) * 0.75;
                return (
                  <>
                    <span className="details-label">Last Seen</span>
                    <span className="details-value details-value--live" id="detail-last-seen">
                      {formatTs(liveTs)}
                      {secsAgo != null && (
                        <span className={`details-sub${isWarning ? ' details-sub--warn' : ' details-sub--live'}`}>
                          {secsAgo}s ago{dropIn != null ? ` · drops in ${dropIn}s` : ''}
                        </span>
                      )}
                    </span>
                  </>
                );
              } else {
                return (
                  <>
                    <span className="details-label">First Seen</span>
                    <span className="details-value" id="detail-first-seen">
                      {formatTs(flightDetail?.start_time)}
                    </span>
                    <span className="details-label">Last Seen</span>
                    <span className="details-value" id="detail-last-seen">
                      {formatTs(flightDetail?.end_time ?? flightDetail?.timestamp)}
                    </span>
                  </>
                );
              }
            })()}
            <span className="details-label">Altitude</span>
            <span className="details-value" id="detail-altitude">
              {lastPoint ? formatAltitude(lastPoint.altitude) : 'N/A'}
            </span>
            <span className="details-label">Speed</span>
            <span className="details-value" id="detail-speed">
              {lastPoint ? formatSpeed(lastPoint.speed) : 'N/A'}
            </span>
            <span className="details-label">Heading</span>
            <span className="details-value" id="detail-heading">
              {lastPoint ? formatHeading(lastPoint.heading) : 'N/A'}
            </span>
            <span className="details-label">Latitude</span>
            <span className="details-value" id="detail-latitude">
              {lastPoint?.latitude != null ? lastPoint.latitude.toFixed(5) : 'N/A'}
            </span>
            <span className="details-label">Longitude</span>
            <span className="details-value" id="detail-longitude">
              {lastPoint?.longitude != null ? lastPoint.longitude.toFixed(5) : 'N/A'}
            </span>
          </div>
        </div>

        <div className="drawer-tabs">
          {flightAlerts.length > 0 && (
            <button
              type="button"
              className={`tab-btn${drawerTab === 'alerts' ? ' active' : ''}`}
              id="tab-btn-alerts"
              onClick={() => onSwitchTab('alerts')}
            >
              Alerts
            </button>
          )}
          <button
            type="button"
            className={`tab-btn${drawerTab === 'telemetry' ? ' active' : ''}`}
            id="tab-btn-telemetry"
            onClick={() => onSwitchTab('telemetry')}
          >
            Telemetry
          </button>
        </div>

        <div
          className="tab-content"
          id="tab-alerts"
          style={{ display: drawerTab === 'alerts' ? 'flex' : 'none' }}
        >
          <div id="alert-timeline-list">
            {sortedAlerts.length === 0 ? (
              <div className="alert-timeline-empty">No alert events for this flight.</div>
            ) : (
              sortedAlerts.map((alert) => {
                const level = formatAlertLevel(alert.level).toLowerCase();
                const timeStr = alert.timestamp
                  ? new Date(Number(alert.timestamp) * 1000).toLocaleTimeString([], {
                      hour: '2-digit',
                      minute: '2-digit',
                      second: '2-digit',
                    })
                  : '';
                const badgeClass = level === 'alert' ? 'alert' : 'warn';
                const displayLvl =
                  formatAlertLevel(alert.level).charAt(0).toUpperCase() +
                  formatAlertLevel(alert.level).slice(1);
                const latVal = alert.latitude != null ? alert.latitude.toFixed(5) : 'N/A';
                const lonVal = alert.longitude != null ? alert.longitude.toFixed(5) : 'N/A';
                const title = `Triggered:\nTime: ${alert.timestamp ? new Date(alert.timestamp * 1000).toLocaleString() : 'N/A'}\nPosition: ${latVal}, ${lonVal}\nAltitude: ${formatAlertAltitude(alert.altitude)}\nETA: ${formatAlertEta(alert.eta)}`;
                return (
                  <div
                    key={alert.alert_id}
                    data-alert-id={alert.alert_id}
                    className={`alert-timeline-item ${level}${alert.alert_id === activeAlertId ? ' active' : ''}`}
                    title={title}
                    onClick={() => onSelectAlert(alert)}
                  >
                    <div className="alert-timeline-row">
                      <span className={`alert-badge ${badgeClass}`}>{displayLvl}</span>
                      <span className="alert-timeline-time">{timeStr}</span>
                    </div>
                    <div className="alert-timeline-zone">
                      Entered Zone: <span className="alert-timeline-zone-name">{alert.zone || 'zone'}</span>
                    </div>
                    <div className="alert-timeline-meta">
                      <span><strong>Alt:</strong> {formatAlertAltitude(alert.altitude)}</span>
                      <span><strong>ETA:</strong> {formatAlertEta(alert.eta)}</span>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        <div
          className="tab-content"
          id="tab-telemetry"
          style={{ display: drawerTab === 'telemetry' ? 'flex' : 'none' }}
        >
          <div className="table-container" ref={tableContainerRef}>
            <table className="tel-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th className="tel-num">Altitude</th>
                  <th className="tel-num">Speed</th>
                  <th className="tel-num">Heading</th>
                  <th className="tel-num">Latitude</th>
                  <th className="tel-num">Longitude</th>
                </tr>
              </thead>
              <tbody id="telemetry-table-body">
                {flightTelemetry.length === 0 ? (
                  <tr>
                    <td colSpan={6} style={{ textAlign: 'center', color: '#64748b' }}>
                      No telemetry data.
                    </td>
                  </tr>
                ) : (
                  flightTelemetry.map((point) => {
                    const timeStr = new Date(point.timestamp * 1000).toLocaleTimeString([], {
                      hour: '2-digit',
                      minute: '2-digit',
                      second: '2-digit',
                    });
                    const latVal = point.latitude != null ? point.latitude.toFixed(4) : 'N/A';
                    const lonVal = point.longitude != null ? point.longitude.toFixed(4) : 'N/A';
                    const isSelected = selectedTelemetryPoint?.timestamp === point.timestamp;
                    return (
                      <tr
                        key={point.timestamp}
                        onClick={() => onSelectTelemetryPoint(point)}
                        className={isSelected ? 'active-tel-row' : ''}
                        style={{ cursor: 'pointer' }}
                      >
                        <td>{timeStr}</td>
                        <td className="tel-num">{formatAltitudeCell(point.altitude)}</td>
                        <td className="tel-num">{formatSpeedCell(point.speed)}</td>
                        <td className="tel-num">{formatHeading(point.heading)}</td>
                        <td className="tel-num">{latVal}</td>
                        <td className="tel-num">{lonVal}</td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}

export type { DrawerTab };
