import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { Alert, AppConfig, FlightDetail, FlightSummary, TelemetryPoint } from '../api/types';
import {
  formatAltitude,
  formatHeading,
  formatSpeed,
  formatZoneLevel,
  isFlightLive,
} from '../utils/format';
import { AlertTimeline } from './AlertTimeline';
import { ExternalLinks } from './ExternalLinks';
import { TelemetryTable } from './TelemetryTable';

type DrawerTab = 'alerts' | 'telemetry';

interface DetailsDrawerProps {
  open: boolean;
  flightDetail: FlightDetail | null;
  flightSummary?: FlightSummary | null;
  activeAlertId: string | null;
  flightAlerts: Alert[];
  flightTelemetry: TelemetryPoint[];
  drawerTab: DrawerTab;
  selectedTelemetryPoint: TelemetryPoint | null;
  appConfig: AppConfig | null;
  selectionError?: string | null;
  isLoading?: boolean;
  onRetry?: () => void;
  onSelectTelemetryPoint: (point: TelemetryPoint) => void;
  onClose: () => void;
  onSwitchTab: (tab: DrawerTab) => void;
  onSelectAlert: (alert: Alert) => void;
}

export function DetailsDrawer({
  open,
  flightDetail,
  flightSummary = null,
  activeAlertId,
  flightAlerts,
  flightTelemetry,
  drawerTab,
  selectedTelemetryPoint,
  appConfig,
  selectionError = null,
  isLoading = false,
  onRetry,
  onSelectTelemetryPoint,
  onClose,
  onSwitchTab,
  onSelectAlert,
}: DetailsDrawerProps) {
  const drawerRef = useRef<HTMLDivElement>(null);
  const tableContainerRef = useRef<HTMLDivElement>(null);
  const [prevTelemetryLength, setPrevTelemetryLength] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!open || !flightDetail || !isFlightLive(flightDetail)) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [open, flightDetail]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    drawerRef.current?.focus();
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, onClose]);

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
      document
        .querySelector('#alert-timeline-list .alert-timeline-item.active')
        ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [activeAlertId, drawerTab, open]);

  const isLoadingPlane = isLoading || (!flightDetail && !selectionError);
  const rawCallsign = (flightDetail?.callsign || flightSummary?.callsign)?.trim();
  const rawIcao = (flightDetail?.icao || flightSummary?.icao)?.toUpperCase() || '';
  const callsign =
    rawCallsign && rawCallsign.toUpperCase() !== 'UNKNOWN'
      ? rawCallsign
      : rawIcao || 'Loading plane details…';
  const icao = rawIcao || 'N/A';
  const photoUrl = typeof flightDetail?.photo_url === 'string' ? flightDetail.photo_url : null;
  const hasPhoto = Boolean(photoUrl);
  const hasAlert = Boolean((flightDetail?.zone || '').trim() || (flightDetail?.level || '').trim());
  const sortedAlerts = [...flightAlerts].sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  const lastPoint = flightTelemetry.length > 0 ? flightTelemetry[flightTelemetry.length - 1] : null;

  return (
    <div
      id="details-drawer"
      ref={drawerRef}
      className={open ? 'open' : ''}
      role="dialog"
      aria-modal="true"
      aria-label="Selected aircraft details"
      tabIndex={-1}
    >
      <button type="button" id="close-drawer-btn" className="close-btn" title="Close" onClick={onClose}>
        &times;
      </button>
      <div id="drawer-header">
        <div className="drawer-header-label">Selected Aircraft</div>
        <h2>
          <span id="detail-callsign">{callsign}</span>{' '}
          {rawIcao && rawIcao !== callsign && (
            <span id="detail-icao" className="flight-icao">
              {icao}
            </span>
          )}
        </h2>
      </div>
      <div className="drawer-content">
        {selectionError ? (
          <div className="info-section">
            <StatusError onRetry={onRetry}>{selectionError}</StatusError>
          </div>
        ) : isLoadingPlane ? (
          <div className="drawer-loading-container">
            <span className="flight-list-spinner" aria-hidden="true" />
            <span>Loading plane details…</span>
          </div>
        ) : (
          <>
            {hasPhoto && photoUrl && (
              <div id="detail-photo-container">
                <img id="detail-photo" src={photoUrl} alt="Aircraft photo" />
                <div className="photo-gradient-bottom">
                  <span>
                    Photo by{' '}
                    <span id="detail-photo-photographer" className="photo-credit-name">
                      {flightDetail?.photo_photographer || 'Planespotters'}
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
            )}

            <ExternalLinks flightDetail={flightDetail} icao={icao} />

            <div className="info-section">
              <h3>Aircraft Details</h3>
              <div className="details-grid">
                <span className="details-label">Registration</span>
                <span className="details-value details-value--accent" id="detail-registration">
                  {flightDetail?.registration && flightDetail.registration !== 'Unknown' ? flightDetail.registration : '—'}
                </span>
                <span className="details-label">Model</span>
                <span className="details-value" id="detail-model">
                  {flightDetail?.model && flightDetail.model !== 'Unknown Model' ? flightDetail.model : '—'}
                </span>
                <span className="details-label">Aircraft Type</span>
                <span className="details-value" id="detail-type">
                  {flightDetail?.aircraft_type || flightDetail?.typecode || '—'}
                </span>
                <span className="details-label">Owner</span>
                <span className="details-value" id="detail-owner">
                  {flightDetail?.owner && flightDetail.owner !== 'Unknown Owner' ? flightDetail.owner : '—'}
                </span>
                <span className="details-label">Registration Country</span>
                <span className="details-value" id="detail-country">
                  {flightDetail?.country && flightDetail.country !== 'Unknown' ? flightDetail.country : '—'}
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
                {renderTelemetrySummary(flightDetail, appConfig, now)}
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

            <div className="drawer-tabs" role="tablist" aria-label="Flight detail tabs">
              {flightAlerts.length > 0 && (
                <button
                  type="button"
                  role="tab"
                  aria-selected={drawerTab === 'alerts'}
                  className={`tab-btn${drawerTab === 'alerts' ? ' active' : ''}`}
                  id="tab-btn-alerts"
                  onClick={() => onSwitchTab('alerts')}
                >
                  Alerts
                </button>
              )}
              <button
                type="button"
                role="tab"
                aria-selected={drawerTab === 'telemetry'}
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
              role="tabpanel"
              hidden={drawerTab !== 'alerts'}
            >
              <AlertTimeline
                alerts={sortedAlerts}
                activeAlertId={activeAlertId}
                onSelectAlert={onSelectAlert}
              />
            </div>

            <div
              className="tab-content"
              id="tab-telemetry"
              role="tabpanel"
              hidden={drawerTab !== 'telemetry'}
            >
              <div ref={tableContainerRef}>
                <TelemetryTable
                  telemetry={flightTelemetry}
                  selectedTelemetryPoint={selectedTelemetryPoint}
                  onSelectTelemetryPoint={onSelectTelemetryPoint}
                />
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function StatusError({ children, onRetry }: { children: ReactNode; onRetry?: () => void }) {
  return (
    <div className="status-error-container">
      <span className="status-message-text">{children}</span>
      {onRetry && (
        <button type="button" className="btn-try-again" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

function renderTelemetrySummary(
  flightDetail: FlightDetail | null,
  appConfig: AppConfig | null,
  now: number,
) {
  const formatTs = (ts?: number | null) =>
    ts
      ? new Date(ts * 1000).toLocaleTimeString([], {
          hour: 'numeric',
          minute: '2-digit',
          second: '2-digit',
        })
      : 'N/A';
  const live = flightDetail ? isFlightLive(flightDetail) : false;
  if (live) {
    const liveTs = flightDetail?.timestamp ?? flightDetail?.end_time ?? flightDetail?.start_time;
    const secsAgo = liveTs ? Math.max(0, Math.round(now / 1000 - liveTs)) : null;
    const rememberSecs = appConfig?.remember_planes ?? null;
    const dropIn =
      secsAgo != null && rememberSecs != null ? Math.max(0, rememberSecs - secsAgo) : null;
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
  }
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

export type { DrawerTab };
