import { useCallback, useState } from 'react';
import type { Alert } from '../api/types';

export function useAlertNotifications() {
  const [toasts, setToasts] = useState<{ id: string; alert: Alert }[]>([]);

  const playWarningChime = useCallback((level: string) => {
    try {
      const AudioCtx = window.AudioContext || (window as any).webkitAudioContext;
      if (!AudioCtx) return;
      const ctx = new AudioCtx();
      const osc1 = ctx.createOscillator();
      const osc2 = ctx.createOscillator();
      const gainNode = ctx.createGain();

      osc1.connect(gainNode);
      osc2.connect(gainNode);
      gainNode.connect(ctx.destination);

      const isAlert = level.toLowerCase() === 'alert';
      osc1.frequency.setValueAtTime(isAlert ? 880 : 587.33, ctx.currentTime);
      osc2.frequency.setValueAtTime(isAlert ? 1046.50 : 698.46, ctx.currentTime);

      gainNode.gain.setValueAtTime(0.12, ctx.currentTime);
      gainNode.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + (isAlert ? 0.7 : 0.4));

      osc1.type = 'sine';
      osc2.type = 'sine';

      osc1.start();
      osc2.start();
      osc1.stop(ctx.currentTime + 0.8);
      osc2.stop(ctx.currentTime + 0.8);
    } catch (err) {
      console.warn('AudioContext warning chime failed:', err);
    }
  }, []);

  const triggerDesktopNotification = useCallback((alert: Alert) => {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    const level = (alert.level || 'warning').toUpperCase();
    new Notification(`PyAerial ${level}: ${alert.callsign || 'Unknown'}`, {
      body: `Zone: ${alert.zone}\nAlt: ${alert.altitude ? alert.altitude + ' m' : 'N/A'}\nETA: ${alert.eta ? Math.round(alert.eta) + 's' : 'N/A'}`,
    });
  }, []);

  const addToast = useCallback((alert: Alert) => {
    const id = `${alert.alert_id}-${Date.now()}-${Math.random()}`;
    setToasts((current) => [{ id, alert }, ...current].slice(0, 5));
    setTimeout(() => {
      setToasts((current) => current.filter((t) => t.id !== id));
    }, 6000);
  }, []);

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
