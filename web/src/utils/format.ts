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

export function formatDuration(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return '0s';
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return secs > 0 ? `${minutes}m ${secs}s` : `${minutes}m`;
  return `${secs}s`;
}

export function formatEpisodeDuration(
  activatedAt?: number,
  deactivatedAt?: number | null,
  now = Date.now() / 1000,
): string {
  if (!activatedAt) return 'N/A';
  const end = deactivatedAt ?? now;
  return formatDuration(Math.max(0, end - activatedAt));
}

export function formatZoneRule(
  zone?: string,
  rule?: string,
  opts?: { live?: boolean },
): string {
  const zoneName = (zone || '').trim() || 'zone';
  const ruleName = (rule || '').trim() || 'rule';
  const base = `${zoneName} · ${ruleName}`;
  return opts?.live ? `${base} (Active)` : base;
}

export function formatFlightAlertSummary(
  flight: {
    is_live?: boolean;
    active_alerts?: { zone?: string; rule?: string; activated_at?: number }[];
    alert_stats?: { episode_count?: number; total_seconds?: number; active_count?: number };
  },
  now = Date.now() / 1000,
): string {
  const active = flight.active_alerts ?? [];
  const stats = flight.alert_stats;
  if (flight.is_live && active.length > 0) {
    return formatActiveAlerts(active, now);
  }
  if (stats && (stats.episode_count ?? 0) > 0) {
    const episodes = stats.episode_count === 1 ? '1 episode' : `${stats.episode_count} episodes`;
    return `${episodes} · ${formatDuration(stats.total_seconds)} alerted`;
  }
  return 'None';
}

export function formatActiveAlerts(
  alerts?: { zone?: string; rule?: string; activated_at?: number }[],
  now = Date.now() / 1000,
): string {
  if (!alerts?.length) return 'None';
  return alerts
    .map((a) => {
      const duration = a.activated_at ? ` (${formatEpisodeDuration(a.activated_at, null, now)})` : '';
      return `${formatZoneRule(a.zone, a.rule)}${duration}`;
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

export function flightAlertSeverity(
  alerts?: { rule?: string }[] | null,
): NormalizedAlertLevel | null {
  if (!alerts?.length) return null;
  if (alerts.some((a) => normalizeAlertRule(a.rule) === 'alert')) return 'alert';
  if (alerts.some((a) => normalizeAlertRule(a.rule) === 'warn')) return 'warn';
  return 'info';
}

export function isAlertActive(alert: { active?: boolean; deactivated_at?: number | null }): boolean {
  if (alert.active === false) return false;
  if (alert.deactivated_at != null && alert.deactivated_at > 0) return false;
  return alert.active === true || alert.deactivated_at == null;
}

export function formatEpisodeTime(
  activatedAt?: number,
  deactivatedAt?: number | null,
  active?: boolean,
): string {
  if (!activatedAt) return 'N/A';
  const isActive = active !== undefined ? active : deactivatedAt == null;
  const startStr = formatActiveSince(activatedAt);
  if (isActive) {
    return `Started ${startStr}`;
  }
  if (deactivatedAt) {
    const endStr = formatActiveSince(deactivatedAt);
    return `${startStr} – ${endStr}`;
  }
  return `Activated ${startStr}`;
}

export function isFlightLive(flight: { is_live?: boolean }): boolean {
  return !!flight.is_live;
}


