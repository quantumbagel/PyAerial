export function isFiniteNumber(v: unknown): boolean {
  if (v === null || v === undefined || v === '') return false;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n);
}

export function formatAltitude(meters: unknown): string {
  if (!isFiniteNumber(meters)) return 'N/A';
  const m = Math.round(Number(meters));
  const ft = Math.round(m * 3.28084);
  return `${m.toLocaleString('en-US')} m (${ft.toLocaleString('en-US')} ft)`;
}

export function formatSpeed(kmh: unknown): string {
  if (!isFiniteNumber(kmh)) return 'N/A';
  const speed = Math.round(Number(kmh));
  const knots = Math.round(speed * 0.539957);
  return `${speed.toLocaleString('en-US')} km/h (${knots.toLocaleString('en-US')} kt)`;
}

export function formatHeading(degrees: unknown): string {
  if (!isFiniteNumber(degrees)) return 'N/A';
  return `${Math.round(Number(degrees)).toLocaleString('en-US')}°`;
}

export function formatAltitudeCell(meters: unknown): string {
  if (!isFiniteNumber(meters)) return 'N/A';
  return `${Math.round(Number(meters)).toLocaleString('en-US')} m`;
}

export function formatSpeedCell(kmh: unknown): string {
  if (!isFiniteNumber(kmh)) return 'N/A';
  return `${Math.round(Number(kmh)).toLocaleString('en-US')} km/h`;
}

export function formatActiveAlerts(alerts?: { zone?: string; rule?: string }[]): string {
  if (!alerts?.length) return 'None';
  return alerts
    .map((a) => {
      const zone = (a.zone || '').trim() || 'zone';
      const rule = (a.rule || '').trim() || 'rule';
      return `${zone} · ${rule}`;
    })
    .join(', ');
}

export function formatActiveSince(ts?: number): string {
  if (!ts) return 'N/A';
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function formatAlertEta(eta: unknown): string {
  return eta != null && Number.isFinite(Number(eta)) ? `${Math.round(Number(eta))} s` : 'N/A';
}

export function formatAlertAltitude(altitude: unknown): string {
  if (altitude == null || !Number.isFinite(Number(altitude))) return 'N/A';
  const m = Math.round(Number(altitude));
  const ft = Math.round(m * 3.28084);
  return `${m} m (${ft} ft)`;
}

export type NormalizedAlertLevel = 'alert' | 'warn' | 'info';

export function normalizeAlertRule(rule?: unknown): NormalizedAlertLevel {
  if (!rule) return 'info';
  const str = String(rule).toLowerCase().trim();
  if (['alert', 'critical', 'danger', 'high', 'error'].includes(str)) {
    return 'alert';
  }
  if (['warn', 'warning', 'medium', 'caution'].includes(str)) {
    return 'warn';
  }
  return 'info';
}

/** @deprecated use normalizeAlertRule */
export const normalizeAlertLevel = normalizeAlertRule;

export function flightAlertSeverity(
  alerts?: { rule?: string }[] | null,
): NormalizedAlertLevel | null {
  if (!alerts?.length) return null;
  if (alerts.some((a) => normalizeAlertRule(a.rule) === 'alert')) return 'alert';
  if (alerts.some((a) => normalizeAlertRule(a.rule) === 'warn')) return 'warn';
  return 'info';
}

export function isFlightLive(flight: { is_live?: boolean }): boolean {
  return !!flight.is_live;
}

