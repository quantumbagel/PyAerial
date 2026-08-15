import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { Alert, AppConfig, FlightDetail, FlightSummary, TelemetryPoint, Zone } from '../api/types';
import {
  formatActiveAlerts,
  formatAltitude,
  formatDateTime,
  formatFlightAlertSummary,
  formatHeading,
  formatSpeed,
  isFlightLive,
} from '../utils/format';
import { AlertTimeline } from './AlertTimeline';
import { ExternalLinks } from './ExternalLinks';
import { LevelBadge } from './LevelBadge';
import { TelemetryTable } from './TelemetryTable';
import { Button, Chip, Spinner, Tab, TabList } from './ui';

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
  zones?: Zone[];
  alertColors?: Record<string, string>;
  selectionError?: string | null;
  isLoading?: boolean;
  onRetry?: () => void;
  onSelectTelemetryPoint: (point: TelemetryPoint) => void;
  onClose: () => void;
  onSwitchTab: (tab: DrawerTab) => void;
  onSelectAlert: (alert: Alert, episodeKey: string) => void;
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
  zones,
  alertColors,
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
      if (event.key !== 'Escape') return;
      // Don't steal Escape from inputs (search box, sort dropdowns, etc.).
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable)) {
        return;
      }
      onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    drawerRef.current?.focus();
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, onClose]);

  useEffect(() => {
    if (!open || drawerTab !== 'telemetry') return;
    const el = tableContainerRef.current;
    if (!el) return;

    const diff = flightTelemetry.length - prevTelemetryLength;
    const oldLength = prevTelemetryLength;
    setPrevTelemetryLength(flightTelemetry.length);

    if (flightTelemetry.length > 0) {
      if (oldLength === 0) {
        el.scrollTop = el.scrollHeight;
      } else if (diff > 0) {
        const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
        if (distanceFromBottom < 100) {
          el.scrollTop = el.scrollHeight;
        }
      }
    }
  }, [flightTelemetry, prevTelemetryLength, open, drawerTab]);

  useEffect(() => {
    if (open && drawerTab === 'telemetry') {
      const el = tableContainerRef.current;
      if (el) {
        requestAnimationFrame(() => {
          el.scrollTop = el.scrollHeight;
        });
      }
    }
  }, [open, drawerTab]);

  useEffect(() => {
    if (activeAlertId && drawerTab === 'alerts' && open) {
      document
        .querySelector('#alert-timeline-list .ui-display.is-active')
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
  const sortedAlerts = [...flightAlerts].sort(
    (a, b) => (a.activated_at || 0) - (b.activated_at || 0),
  );
  const lastPoint = flightTelemetry.length > 0 ? flightTelemetry[flightTelemetry.length - 1] : null;
  const displayFlight = useMemo(() => {
    const base = flightDetail ?? flightSummary;
    if (!base) return null;
    if (!flightSummary?.is_live) {
      return base;
    }
    return {
      ...base,
      latitude: flightSummary.latitude ?? base.latitude,
      longitude: flightSummary.longitude ?? base.longitude,
      heading: flightSummary.heading ?? base.heading,
      speed: flightSummary.speed ?? base.speed,
      altitude: flightSummary.altitude ?? base.altitude,
    };
  }, [flightDetail, flightSummary]);

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
      <div id="drawer-header">
        <Button
          id="close-drawer-btn"
          variant="ghost"
          className="drawer-close-btn"
          title="Close"
          onClick={onClose}
        >
          &times;
        </Button>
        <div className="drawer-header-label">Selected Aircraft</div>
        <h2>
          <span id="detail-callsign">{callsign}</span>{' '}
          {rawIcao && rawIcao !== callsign && (
            <Chip id="detail-icao">{icao}</Chip>
          )}
          {displayFlight && (
            <LevelBadge flight={displayFlight} zones={zones} alertColors={alertColors} />
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
            <Spinner size="xl" />
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
              <div className="ui-fields">
                <span className="ui-field-label">Registration</span>
                <span className="ui-field-value ui-field-value--emphasis" id="detail-registration">
                  {flightDetail?.registration && flightDetail.registration !== 'Unknown' ? flightDetail.registration : '—'}
                </span>
                <span className="ui-field-label">Model</span>
                <span className="ui-field-value" id="detail-model">
                  {flightDetail?.model && flightDetail.model !== 'Unknown Model' ? flightDetail.model : '—'}
                </span>
                <span className="ui-field-label">Aircraft Type</span>
                <span className="ui-field-value" id="detail-type">
                  {flightDetail?.aircraft_type || '—'}
                </span>
                <span className="ui-field-label">Owner</span>
                <span className="ui-field-value" id="detail-owner">
                  {flightDetail?.owner && flightDetail.owner !== 'Unknown Owner' ? flightDetail.owner : '—'}
                </span>
                <span className="ui-field-label">Registration Country</span>
                <span className="ui-field-value" id="detail-country">
                  {flightDetail?.country && flightDetail.country !== 'Unknown' ? flightDetail.country : '—'}
                </span>
                <span className="ui-field-label">
                  {isFlightLive(flightDetail ?? {}) ? 'Active Alerts' : 'Alert Summary'}
                </span>
                <span
                  className={`ui-field-value${
                    (flightDetail?.active_alerts?.length ?? 0) > 0
                      ? ' ui-field-value--warn'
                      : (flightDetail?.alert_stats?.episode_count ?? 0) > 0
                      ? ' ui-field-value--accent'
                      : ' ui-field-value--muted'
                  }`}
                  id="detail-active-alerts"
                >
                  {isFlightLive(flightDetail ?? {})
                    ? (flightDetail?.active_alerts?.length ?? 0) > 0
                      ? formatActiveAlerts(flightDetail?.active_alerts)
                      : (flightDetail?.alert_stats?.episode_count ?? 0) > 0
                      ? `None (${flightDetail?.alert_stats?.episode_count} past episode${
                          flightDetail?.alert_stats?.episode_count === 1 ? '' : 's'
                        })`
                      : 'None'
                    : formatFlightAlertSummary(flightDetail ?? {})}
                </span>
              </div>
            </div>

            <div className="info-section">
              <h3>Telemetry Readings</h3>
              <div className="ui-fields">
                {renderTelemetrySummary(
                  flightDetail,
                  flightSummary,
                  flightTelemetry,
                  appConfig,
                  now,
                )}
                <span className="ui-field-label">Altitude</span>
                <span className="ui-field-value" id="detail-altitude">
                  {lastPoint ? formatAltitude(lastPoint.altitude) : 'N/A'}
                </span>
                <span className="ui-field-label">Speed</span>
                <span className="ui-field-value" id="detail-speed">
                  {lastPoint ? formatSpeed(lastPoint.speed) : 'N/A'}
                </span>
                <span className="ui-field-label">Heading</span>
                <span className="ui-field-value" id="detail-heading">
                  {lastPoint ? formatHeading(lastPoint.heading) : 'N/A'}
                </span>
                <span className="ui-field-label">Latitude</span>
                <span className="ui-field-value" id="detail-latitude">
                  {lastPoint?.latitude != null ? lastPoint.latitude.toFixed(5) : 'N/A'}
                </span>
                <span className="ui-field-label">Longitude</span>
                <span className="ui-field-value" id="detail-longitude">
                  {lastPoint?.longitude != null ? lastPoint.longitude.toFixed(5) : 'N/A'}
                </span>
              </div>
            </div>

            <TabList aria-label="Flight detail tabs">
              {flightAlerts.length > 0 && (
                <Tab
                  id="tab-btn-alerts"
                  active={drawerTab === 'alerts'}
                  onClick={() => onSwitchTab('alerts')}
                >
                  Alerts
                </Tab>
              )}
              <Tab
                id="tab-btn-telemetry"
                active={drawerTab === 'telemetry'}
                onClick={() => onSwitchTab('telemetry')}
              >
                Telemetry
              </Tab>
            </TabList>

            <div
              className="tab-content"
              id="tab-alerts"
              role="tabpanel"
              hidden={drawerTab !== 'alerts'}
            >
              <AlertTimeline
                alerts={sortedAlerts}
                activeAlertId={activeAlertId}
                zones={zones}
                alertColors={alertColors}
                onSelectAlert={onSelectAlert}
              />
            </div>

            <div
              className="tab-content"
              id="tab-telemetry"
              role="tabpanel"
              hidden={drawerTab !== 'telemetry'}
            >
              <TelemetryTable
                telemetry={flightTelemetry}
                selectedTelemetryPoint={selectedTelemetryPoint}
                onSelectTelemetryPoint={onSelectTelemetryPoint}
                containerRef={tableContainerRef}
              />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function StatusError({ children, onRetry }: { children: ReactNode; onRetry?: () => void }) {
  return (
    <div className="ui-empty ui-empty--error">
      <span>{children}</span>
      {onRetry && (
        <Button variant="primary" size="md" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

function renderTelemetrySummary(
  flightDetail: FlightDetail | null,
  flightSummary: FlightSummary | null,
  flightTelemetry: TelemetryPoint[],
  appConfig: AppConfig | null,
  now: number,
) {
  const formatTs = (ts?: number | null) =>
    ts ? formatDateTime(ts, { withSeconds: true }) : 'N/A';
  const live = flightDetail
    ? isFlightLive(flightDetail)
    : flightSummary
    ? isFlightLive(flightSummary)
    : false;

  const firstTsCandidates = [
    flightDetail?.start_time,
    flightSummary?.start_time,
    flightTelemetry.length > 0 ? flightTelemetry[0].timestamp : null,
  ].filter((ts): ts is number => typeof ts === 'number' && ts > 0);
  const firstTs = firstTsCandidates.length > 0 ? Math.min(...firstTsCandidates) : null;

  const lastPoint = flightTelemetry.length > 0 ? flightTelemetry[flightTelemetry.length - 1] : null;

  const lastTsCandidates = [
    lastPoint?.timestamp,
    flightSummary?.timestamp,
    flightSummary?.end_time,
    flightDetail?.timestamp,
    flightDetail?.end_time,
    flightDetail?.start_time,
    flightSummary?.start_time,
  ].filter((ts): ts is number => typeof ts === 'number' && ts > 0);
  const lastTs = lastTsCandidates.length > 0 ? Math.max(...lastTsCandidates) : null;

  if (live) {
    const secsAgo = lastTs ? Math.max(0, Math.round(now / 1000 - lastTs)) : null;
    const rememberSecs = appConfig?.remember_planes ?? null;
    const dropIn =
      secsAgo != null && rememberSecs != null ? Math.max(0, rememberSecs - secsAgo) : null;
    return (
      <>
        <span className="ui-field-label">First Seen</span>
        <span className="ui-field-value" id="detail-first-seen">
          {formatTs(firstTs)}
        </span>
        <span className="ui-field-label">Last Seen</span>
        <span className="ui-field-value ui-field-value--emphasis" id="detail-last-seen">
          {formatTs(lastTs)}
          {secsAgo != null && (
            <span className="ui-field-sub">
              {secsAgo}s ago{dropIn != null ? ` · drops in ${dropIn}s` : ''}
            </span>
          )}
        </span>
      </>
    );
  }
  return (
    <>
      <span className="ui-field-label">First Seen</span>
      <span className="ui-field-value" id="detail-first-seen">
        {formatTs(firstTs)}
      </span>
      <span className="ui-field-label">Last Seen</span>
      <span className="ui-field-value" id="detail-last-seen">
        {formatTs(lastTs)}
      </span>
    </>
  );
}

export type { DrawerTab };
