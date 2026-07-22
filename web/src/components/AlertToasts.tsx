import { useEffect, useRef, useState } from 'react';
import type { Alert } from '../api/types';
import type { AlertToastItem } from '../hooks/useAlertNotifications';
import {
  formatActiveSince,
  formatAlertAltitude,
  formatAlertEta,
  formatEpisodeDuration,
  isAlertActive,
  normalizeAlertRule,
} from '../utils/format';
import { AlertRuleBadge, AlertStatusBadge } from './LevelBadge';

interface AlertToastsProps {
  toasts: AlertToastItem[];
  onSelectAlert: (alert: Alert) => void;
  onDismiss: (id: string) => void;
}

interface ToastItemProps {
  id: string;
  alert: Alert;
  eventType?: 'activated' | 'deactivated';
  duration?: number;
  onSelectAlert: (alert: Alert) => void;
  onDismiss: (id: string) => void;
}

function ToastItem({
  id,
  alert,
  eventType = 'activated',
  duration = 6000,
  onSelectAlert,
  onDismiss,
}: ToastItemProps) {
  const [remainingMs, setRemainingMs] = useState(duration);
  const [isPaused, setIsPaused] = useState(false);
  const deadlineRef = useRef(Date.now() + duration);
  const normLevel = normalizeAlertRule(alert.rule);
  const isActivated = eventType === 'activated';
  const isEpisodeActive = isAlertActive(alert);

  useEffect(() => {
    if (isPaused) return;
    const tick = () => {
      const nextRemaining = Math.max(0, deadlineRef.current - Date.now());
      setRemainingMs(nextRemaining);
      if (nextRemaining <= 0) {
        onDismiss(id);
      }
    };
    tick();
    const timer = window.setInterval(tick, 100);
    return () => window.clearInterval(timer);
  }, [id, isPaused, onDismiss]);

  const handlePause = () => {
    if (isPaused) return;
    setIsPaused(true);
    setRemainingMs(Math.max(0, deadlineRef.current - Date.now()));
  };

  const handleResume = () => {
    if (!isPaused) return;
    deadlineRef.current = Date.now() + remainingMs;
    setIsPaused(false);
  };

  const rawCallsign = alert.callsign?.trim();
  const callsign =
    rawCallsign && rawCallsign.toUpperCase() !== 'UNKNOWN'
      ? rawCallsign
      : alert.icao?.toUpperCase().trim() || 'Loading plane details…';
  const timeStr = formatActiveSince(alert.activated_at);
  const zoneRule = `${alert.zone || 'zone'} · ${alert.rule || 'rule'}`;
  const durationStr = formatEpisodeDuration(alert.activated_at, alert.deactivated_at);

  return (
    <div
      className={`toast-alert level-${normLevel}${!isActivated ? ' toast-cleared' : ''}${isPaused ? ' paused' : ''}`}
      onMouseEnter={handlePause}
      onMouseLeave={handleResume}
      onClick={() => {
        onSelectAlert(alert);
        onDismiss(id);
      }}
    >
      <div className="toast-header">
        <div className="toast-header-left">
          <AlertRuleBadge rule={alert.rule} />
          {isActivated ? (
            <AlertStatusBadge active={isEpisodeActive} />
          ) : (
            <span className="alert-status-badge ended">CLEARED</span>
          )}
          <span className="toast-callsign">{callsign}</span>
        </div>
        <div className="toast-header-right">
          {timeStr && <span className="toast-time">{timeStr}</span>}
          <button
            type="button"
            className="toast-close"
            aria-label="Dismiss notification"
            onClick={(e) => {
              e.stopPropagation();
              onDismiss(id);
            }}
          >
            <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
      </div>
      <div className="toast-body">
        <div className="toast-zone-row">
          <span className="toast-zone-label">{isActivated ? 'Alert:' : 'Cleared:'}</span>
          <span className="toast-zone-val">{zoneRule}</span>
        </div>
        <div className="toast-metrics">
          {isActivated ? (
            <>
              <span>Alt: {formatAlertAltitude(alert.altitude)}</span>
              <span>ETA: {formatAlertEta(alert.eta)}</span>
            </>
          ) : (
            <>
              <span>Total Episode: {durationStr}</span>
              <span>Alt: {formatAlertAltitude(alert.altitude)}</span>
            </>
          )}
        </div>
      </div>
      <div className="toast-action-hint">
        {isActivated ? 'Click to inspect live flight →' : 'Click to view episode details →'}
      </div>
      <div
        className="toast-progress-bar"
        style={{
          animationDuration: `${duration}ms`,
          animationPlayState: isPaused ? 'paused' : 'running',
          transform: `scaleX(${Math.max(0, remainingMs / duration)})`,
        }}
      />
    </div>
  );
}

export function AlertToasts({ toasts, onSelectAlert, onDismiss }: AlertToastsProps) {
  return (
    <div className="toast-container" role="region" aria-label="Notifications" aria-live="polite">
      {toasts.map((toast) => (
        <ToastItem
          key={toast.id}
          id={toast.id}
          alert={toast.alert}
          eventType={toast.eventType}
          duration={toast.duration}
          onSelectAlert={onSelectAlert}
          onDismiss={onDismiss}
        />
      ))}
    </div>
  );
}

