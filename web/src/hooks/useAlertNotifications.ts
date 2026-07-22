import { useCallback, useState } from 'react';
import type { Alert } from '../api/types';
import { formatAlertAltitude, formatAlertEta, normalizeAlertRule } from '../utils/format';

let sharedAudioCtx: AudioContext | null = null;

function getAudioContext(): AudioContext | null {
  if (typeof window === 'undefined') return null;
  try {
    const AudioCtx = window.AudioContext || (window as any).webkitAudioContext;
    if (!AudioCtx) return null;
    if (!sharedAudioCtx || sharedAudioCtx.state === 'closed') {
      sharedAudioCtx = new AudioCtx();
    }
    if (sharedAudioCtx.state === 'suspended') {
      sharedAudioCtx.resume().catch(() => {});
    }
    return sharedAudioCtx;
  } catch {
    return null;
  }
}

export interface AlertToastItem {
  id: string;
  alert: Alert;
  eventType: 'activated' | 'deactivated';
  duration?: number;
}

export function useAlertNotifications() {
  const [toasts, setToasts] = useState<AlertToastItem[]>([]);

  const playWarningChime = useCallback((rule: string) => {
    try {
      const ctx = getAudioContext();
      if (!ctx) return;

      const norm = normalizeAlertRule(rule);
      const now = ctx.currentTime;

      const osc1 = ctx.createOscillator();
      const osc2 = ctx.createOscillator();
      const gainNode = ctx.createGain();

      osc1.connect(gainNode);
      osc2.connect(gainNode);
      gainNode.connect(ctx.destination);

      if (norm === 'alert') {
        osc1.type = 'triangle';
        osc2.type = 'sine';
        osc1.frequency.setValueAtTime(880, now);
        osc2.frequency.setValueAtTime(1046.5, now);
        gainNode.gain.setValueAtTime(0.14, now);
        gainNode.gain.exponentialRampToValueAtTime(0.001, now + 0.75);
        osc1.start(now);
        osc2.start(now);
        osc1.stop(now + 0.75);
        osc2.stop(now + 0.75);
      } else if (norm === 'warn') {
        osc1.type = 'sine';
        osc2.type = 'sine';
        osc1.frequency.setValueAtTime(587.33, now);
        osc2.frequency.setValueAtTime(698.46, now);
        gainNode.gain.setValueAtTime(0.1, now);
        gainNode.gain.exponentialRampToValueAtTime(0.001, now + 0.45);
        osc1.start(now);
        osc2.start(now);
        osc1.stop(now + 0.45);
        osc2.stop(now + 0.45);
      } else {
        osc1.type = 'sine';
        osc2.type = 'sine';
        osc1.frequency.setValueAtTime(523.25, now);
        osc2.frequency.setValueAtTime(659.25, now);
        gainNode.gain.setValueAtTime(0.06, now);
        gainNode.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
        osc1.start(now);
        osc2.start(now);
        osc1.stop(now + 0.35);
        osc2.stop(now + 0.35);
      }
    } catch (err) {
      console.warn('AudioContext warning chime failed:', err);
    }
  }, []);

  const triggerDesktopNotification = useCallback(
    (
      alert: Alert,
      eventType: 'activated' | 'deactivated' = 'activated',
      onSelectAlert?: (alert: Alert) => void,
    ) => {
      if (!('Notification' in window) || Notification.permission !== 'granted') return;
      const normLevel = normalizeAlertRule(alert.rule);
      const isActivated = eventType === 'activated';
      const icon = isActivated
        ? normLevel === 'alert'
          ? '🚨'
          : normLevel === 'warn'
          ? '⚠️'
          : 'ℹ️'
        : '✅';
      const displayLevel = normLevel === 'alert' ? 'ALERT' : normLevel === 'warn' ? 'WARNING' : 'INFO';
      const statusTitle = isActivated ? `LIVE ${displayLevel}` : `ALERT CLEARED`;
      const rawCallsign = alert.callsign?.trim();
      const callsignStr =
        rawCallsign && rawCallsign.toUpperCase() !== 'UNKNOWN'
          ? rawCallsign
          : alert.icao
          ? alert.icao.toUpperCase()
          : 'Aircraft';
      const alertStr = `${alert.zone || 'zone'} · ${alert.rule || 'rule'}`;
      const altStr = `Alt: ${formatAlertAltitude(alert.altitude)}`;
      const etaStr = `ETA: ${formatAlertEta(alert.eta)}`;
      const body = isActivated
        ? `${alertStr} • ${altStr} • ${etaStr}`
        : `${alertStr} • Hazard condition ended`;

      const notification = new Notification(`${icon} PyAerial ${statusTitle}: ${callsignStr}`, {
        body,
        tag: `${alert.alert_id}-${eventType}`,
      });

      notification.onclick = () => {
        window.focus();
        if (onSelectAlert) {
          onSelectAlert(alert);
        }
      };
    },
    [],
  );

  const addToast = useCallback(
    (alert: Alert, eventType: 'activated' | 'deactivated' = 'activated') => {
      const id = `${alert.alert_id}-${eventType}-${Date.now()}-${Math.random()}`;
      const norm = normalizeAlertRule(alert.rule);
      const duration = eventType === 'deactivated' ? 5000 : norm === 'alert' ? 8000 : 6000;
      setToasts((current) => [{ id, alert, eventType, duration }, ...current].slice(0, 5));
    },
    [],
  );

  const dismissToast = useCallback((id: string) => {
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  return {
    toasts,
    addToast,
    dismissToast,
    playWarningChime,
    triggerDesktopNotification,
  };
}

