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

export function formatZoneLevel(zone?: string, level?: string): string {
  const z = (zone || '').trim();
  const l = (level || '').trim();
  if (!z && !l) return 'N/A';
  return `${z || 'N/A'} / ${l || 'N/A'}`;
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

export function normalizeAlertLevel(level?: unknown): NormalizedAlertLevel {
  if (!level) return 'info';
  const str = String(level).toLowerCase().trim();
  if (['alert', 'critical', 'danger', 'high', 'error'].includes(str)) {
    return 'alert';
  }
  if (['warn', 'warning', 'medium', 'caution'].includes(str)) {
    return 'warn';
  }
  return 'info';
}

export function isFlightLive(flight: { is_live?: boolean }): boolean {
  return !!flight.is_live;
}

