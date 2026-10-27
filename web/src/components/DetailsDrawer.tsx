import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { Alert, AppConfig, FlightDetail, FlightSummary, TelemetryPoint, Zone } from '../api/types';
import { formatActiveAlerts, formatAlertHistory } from '../utils/alertData';
import {
  EMPTY,
  LIST_SEP,
  displayCallsign,
  displayField,
  displayIcao,
  formatAgo,
  formatAltitude,
  formatCoordinate,
  formatDateTime,
  formatDuration,
  formatHeading,
  formatSpeed,
  isFlightLive,
  safeHttpsUrl,
} from '../utils/format';
import { FlightAlertBadges } from './AlertBadges';
import { AlertTimeline } from './AlertTimeline';
import { ExternalLinks } from './ExternalLinks';
import { IntegritySection } from './IntegritySection';
import { TelemetryTable } from './TelemetryTable';
import { Button, Chip, StateView, Tab, TabList, TabPanel, useTabIds } from './ui';

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
  onSelectAlert: (alert: Alert) => void;
}

/** Live flights: merge the fresher list summary over the fetched detail. */
function mergeDisplayFlight(detail: FlightDetail | null, summary: FlightSummary | null) {
  const base = detail ?? summary;
  if (!base || !summary?.is_live) return base;
  return {
    ...base,
    latitude: summary.latitude ?? base.latitude,
    longitude: summary.longitude ?? base.longitude,
    heading: summary.heading ?? base.heading,
    speed: summary.speed ?? base.speed,
    altitude: summary.altitude ?? base.altitude,
    active_alerts: summary.active_alerts ?? base.active_alerts,
    alert_stats: summary.alert_stats ?? base.alert_stats,
    integrity: summary.integrity ?? base.integrity,
  };
}

function seenRange(
  detail: FlightDetail | null,
  summary: FlightSummary | null,
  telemetry: TelemetryPoint[],
): { first: number | null; last: number | null } {
  const valid = (values: (number | null | undefined)[]) =>
    values.filter((ts): ts is number => typeof ts === 'number' && ts > 0);
  const firsts = valid([detail?.start_time, summary?.start_time, telemetry[0]?.timestamp]);
  const lasts = valid([
    telemetry[telemetry.length - 1]?.timestamp,
    summary?.timestamp,
    summary?.end_time,
    detail?.timestamp,
    detail?.end_time,
    ...firsts,
  ]);
  return {
    first: firsts.length ? Math.min(...firsts) : null,
    last: lasts.length ? Math.max(...lasts) : null,
  };
}

type Kinematics = Pick<TelemetryPoint, 'altitude' | 'speed' | 'heading' | 'latitude' | 'longitude'>;

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
  const drawerRef = useRef<HTMLElement>(null);
  const tableContainerRef = useRef<HTMLDivElement>(null);
  const alertListRef = useRef<HTMLDivElement>(null);
  const headingId = useId();
  const tabIds = useTabIds();
  const [prevTelemetryLength, setPrevTelemetryLength] = useState(0);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const displayFlight = useMemo(
    () => mergeDisplayFlight(flightDetail, flightSummary),
    [flightDetail, flightSummary],
  );
  const live = displayFlight ? isFlightLive(displayFlight) : false;

  useEffect(() => {
    if (!open || !live) return undefined;
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, [open, live]);

  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  // Escape closes the drawer unless something inside handled it first (an
  // open sort menu, an input, ...).
  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.tagName === 'SELECT' ||
          target.isContentEditable)
      ) {
        return;
      }
      onCloseRef.current();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open]);

  // Move focus in only when the drawer opens, and hand it back when it closes.
  const returnFocusRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const drawer = drawerRef.current;
    if (!drawer) return undefined;
    if (open) {
      const active = document.activeElement;
      returnFocusRef.current =
        active instanceof HTMLElement && active !== document.body && !drawer.contains(active)
          ? active
          : null;
      drawer.focus({ preventScroll: true });
      return undefined;
    }
    const target = returnFocusRef.current;
    returnFocusRef.current = null;
    const active = document.activeElement;
    const focusWasInside = !active || active === document.body || drawer.contains(active);
    if (target && target.isConnected && focusWasInside) {
      target.focus({ preventScroll: true });
    }
    return undefined;
  }, [open]);

  // Keep the telemetry table pinned to the newest row unless the user scrolled up.
  useEffect(() => {
    if (!open || drawerTab !== 'telemetry') return;
    const el = tableContainerRef.current;
    const oldLength = prevTelemetryLength;
    setPrevTelemetryLength(flightTelemetry.length);
    if (!el || flightTelemetry.length === 0) return;
    if (oldLength === 0) {
      el.scrollTop = el.scrollHeight;
    } else if (flightTelemetry.length > oldLength) {
      const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (distanceFromBottom < 100) el.scrollTop = el.scrollHeight;
    }
  }, [flightTelemetry, prevTelemetryLength, open, drawerTab]);

  useEffect(() => {
    if (!open || drawerTab !== 'telemetry') return undefined;
    const el = tableContainerRef.current;
    if (!el) return undefined;
    const frame = requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [open, drawerTab]);

  useEffect(() => {
    if (!activeAlertId || drawerTab !== 'alerts' || !open) return;
    alertListRef.current
      ?.querySelector('.ui-display.is-active')
      ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [activeAlertId, drawerTab, open]);

  const isLoadingPlane = isLoading || (!flightDetail && !selectionError);
  const identity = flightDetail ?? flightSummary;
  const title = identity ? displayCallsign(identity) : EMPTY;
  const icao = identity?.icao ?? null;
  const photoUrl = safeHttpsUrl(flightDetail?.photo_url);
  const photoLink = safeHttpsUrl(flightDetail?.photo_link);
  const sortedAlerts = useMemo(
    () => [...flightAlerts].sort((a, b) => (a.activated_at || 0) - (b.activated_at || 0)),
    [flightAlerts],
  );
  const lastPoint = flightTelemetry.length > 0 ? flightTelemetry[flightTelemetry.length - 1] : null;
  const readingPoint: Kinematics | null = selectedTelemetryPoint ?? lastPoint;
  const reading = (key: keyof Kinematics) => readingPoint?.[key] ?? displayFlight?.[key];
  const { first: firstSeen, last: lastSeen } = seenRange(flightDetail, flightSummary, flightTelemetry);
  const remember = appConfig?.remember_planes ?? null;
  const secsAgo = lastSeen != null ? Math.max(0, now - lastSeen) : null;
  const activeAlerts = displayFlight?.active_alerts ?? [];
  const pastEpisodes = displayFlight?.alert_stats?.episode_count ?? 0;
  const hasAlertsTab = flightAlerts.length > 0;
  const shownTab: DrawerTab = drawerTab === 'alerts' && !hasAlertsTab ? 'telemetry' : drawerTab;

  const alertSummary = activeAlerts.length
    ? formatActiveAlerts(activeAlerts, now)
    : pastEpisodes > 0
      ? formatAlertHistory(displayFlight?.alert_stats)
      : 'None';
  const alertTone = activeAlerts.length ? 'warn' : pastEpisodes > 0 ? 'accent' : 'muted';

  return (
    <aside
      ref={drawerRef}
      className={`details-drawer${open ? ' is-open' : ''}`}
      aria-labelledby={headingId}
      aria-hidden={!open}
      inert={!open ? true : undefined}
      tabIndex={-1}
    >
      <header className="drawer-header">
        <Button
          variant="ghost"
          className="drawer-close-btn"
          title="Close"
          aria-label="Close aircraft details"
          onClick={onClose}
        >
          <span aria-hidden="true">&times;</span>
        </Button>
        <div className="drawer-header-label">Selected aircraft</div>
        <h2 id={headingId} className="drawer-title">
          <span>{title}</span>
          {icao && identity?.callsign ? <Chip>{displayIcao(icao)}</Chip> : null}
          {displayFlight && (
            <FlightAlertBadges flight={displayFlight} zones={zones} alertColors={alertColors} />
          )}
        </h2>
      </header>
      <div className="drawer-content">
        {selectionError ? (
          <StateView kind="error" onRetry={onRetry}>
            {selectionError}
          </StateView>
        ) : isLoadingPlane ? (
          <StateView kind="loading">Loading aircraft details…</StateView>
        ) : (
          <>
            {photoUrl && (
              <figure className="drawer-photo">
                <img
                  src={photoUrl}
                  alt={`Photo of ${title}`}
                  onError={(event) => {
                    const fig = event.currentTarget.closest('figure');
                    if (fig) fig.hidden = true;
                  }}
                />
                <figcaption className="drawer-photo__caption">
                  <span>
                    Photo by{' '}
                    <span className="drawer-photo__credit">
                      {flightDetail?.photo_photographer ?? 'Planespotters'}
                    </span>
                  </span>
                  {photoLink && (
                    <a className="drawer-photo__link" href={photoLink} target="_blank" rel="noreferrer">
                      View on Planespotters.net
                    </a>
                  )}
                </figcaption>
              </figure>
            )}

            <ExternalLinks flightDetail={flightDetail} icao={icao} />

            <section className="info-section">
              <h3>Aircraft details</h3>
              <dl className="ui-fields">
                <dt className="ui-field-label">Registration</dt>
                <dd className="ui-field-value ui-field-value--emphasis">
                  {displayField(flightDetail?.registration)}
                </dd>
                <dt className="ui-field-label">Model</dt>
                <dd className="ui-field-value">{displayField(flightDetail?.model)}</dd>
                <dt className="ui-field-label">Aircraft type</dt>
                <dd className="ui-field-value">{displayField(flightDetail?.aircraft_type)}</dd>
                <dt className="ui-field-label">Owner</dt>
                <dd className="ui-field-value">{displayField(flightDetail?.owner)}</dd>
                <dt className="ui-field-label">Registration country</dt>
                <dd className="ui-field-value">{displayField(flightDetail?.country)}</dd>
                <dt className="ui-field-label">{live ? 'Active alerts' : 'Alerts'}</dt>
                <dd className={`ui-field-value ui-field-value--${alertTone}`}>{alertSummary}</dd>
              </dl>
            </section>

            <IntegritySection integrity={displayFlight?.integrity} />

            <section className="info-section">
              <h3>{selectedTelemetryPoint ? 'Selected fix' : 'Latest readings'}</h3>
              <dl className="ui-fields">
                <dt className="ui-field-label">First seen</dt>
                <dd className="ui-field-value">{formatDateTime(firstSeen, { withSeconds: true })}</dd>
                <dt className="ui-field-label">Last seen</dt>
                <dd className={`ui-field-value${live ? ' ui-field-value--emphasis' : ''}`}>
                  {formatDateTime(lastSeen, { withSeconds: true })}
                  {live && secsAgo != null && (
                    <span className="ui-field-sub">
                      {formatAgo(lastSeen, now)}
                      {remember != null
                        ? `${LIST_SEP}drops in ${formatDuration(Math.max(0, remember - secsAgo))}`
                        : ''}
                    </span>
                  )}
                </dd>
                {selectedTelemetryPoint && (
                  <>
                    <dt className="ui-field-label">Fix time</dt>
                    <dd className="ui-field-value">
                      {formatDateTime(selectedTelemetryPoint.timestamp, { withSeconds: true })}
                    </dd>
                  </>
                )}
                <dt className="ui-field-label">Altitude</dt>
                <dd className="ui-field-value">{formatAltitude(reading('altitude'))}</dd>
                <dt className="ui-field-label">Speed</dt>
                <dd className="ui-field-value">{formatSpeed(reading('speed'))}</dd>
                <dt className="ui-field-label">Heading</dt>
                <dd className="ui-field-value">{formatHeading(reading('heading'))}</dd>
                <dt className="ui-field-label">Latitude</dt>
                <dd className="ui-field-value">{formatCoordinate(reading('latitude'))}</dd>
                <dt className="ui-field-label">Longitude</dt>
                <dd className="ui-field-value">{formatCoordinate(reading('longitude'))}</dd>
              </dl>
            </section>

            <TabList aria-label="Flight detail views">
              {hasAlertsTab && (
                <Tab
                  id={tabIds.tab('alerts')}
                  controls={tabIds.panel('alerts')}
                  active={shownTab === 'alerts'}
                  onClick={() => onSwitchTab('alerts')}
                >
                  Alerts
                </Tab>
              )}
              <Tab
                id={tabIds.tab('telemetry')}
                controls={tabIds.panel('telemetry')}
                active={shownTab === 'telemetry'}
                onClick={() => onSwitchTab('telemetry')}
              >
                Telemetry
              </Tab>
            </TabList>

            {hasAlertsTab && (
              <TabPanel
                id={tabIds.panel('alerts')}
                labelledBy={tabIds.tab('alerts')}
                active={shownTab === 'alerts'}
                className="tab-content"
              >
                <div ref={alertListRef}>
                  <AlertTimeline
                    alerts={sortedAlerts}
                    activeAlertId={activeAlertId}
                    zones={zones}
                    alertColors={alertColors}
                    onSelectAlert={onSelectAlert}
                  />
                </div>
              </TabPanel>
            )}
            <TabPanel
              id={tabIds.panel('telemetry')}
              labelledBy={tabIds.tab('telemetry')}
              active={shownTab === 'telemetry'}
              className="tab-content"
            >
              <TelemetryTable
                telemetry={flightTelemetry}
                selectedTelemetryPoint={selectedTelemetryPoint}
                onSelectTelemetryPoint={onSelectTelemetryPoint}
                containerRef={tableContainerRef}
              />
            </TabPanel>
          </>
        )}
      </div>
    </aside>
  );
}

export type { DrawerTab };
