import type { Alert, TelemetryPoint } from '../api/types';
import type { NormalizedAlertLevel } from './format';
import { normalizeAlertRule } from './format';
import { alertEpisodeIdentity } from './alertData';

export interface AlertPathSegment {
  latlngs: [number, number][];
  severity: NormalizedAlertLevel;
  rule?: string;
  zone?: string;
}

export interface NormalizedAlertEpisode {
  alert_id: string;
  zone?: string;
  rule?: string;
  activated_at: number;
  deactivated_at: number | null;
  active?: boolean;
}

export function normalizeAlertEpisodes(alerts: Alert[]): NormalizedAlertEpisode[] {
  const byId = new Map<string, NormalizedAlertEpisode>();
  for (const alert of alerts) {
    const key = alertEpisodeIdentity(alert);
    if (!key) continue;
    const existing = byId.get(key);
    const activated = alert.activated_at ?? 0;
    const deactivated = alert.deactivated_at ?? null;
    const isEnded = alert.active === false || deactivated != null;
    if (!existing) {
      byId.set(key, {
        alert_id: alert.alert_id || key,
        zone: alert.zone,
        rule: alert.rule,
        activated_at: activated,
        deactivated_at: deactivated,
        active: !isEnded,
      });
      continue;
    }
    const mergedDeactivated =
      existing.deactivated_at == null && deactivated == null
        ? null
        : Math.max(existing.deactivated_at ?? 0, deactivated ?? 0);
    byId.set(key, {
      ...existing,
      zone: existing.zone || alert.zone,
      rule: existing.rule || alert.rule,
      activated_at: Math.min(existing.activated_at || activated, activated || existing.activated_at),
      deactivated_at: mergedDeactivated,
      active: isEnded ? false : (existing.active && alert.active !== false),
    });
  }
  return [...byId.values()].sort((a, b) => a.activated_at - b.activated_at);
}

function episodeActiveAt(
  episode: NormalizedAlertEpisode,
  timestamp: number,
  flightEnd: number,
): boolean {
  if (timestamp < episode.activated_at) return false;
  const end = episode.deactivated_at ?? (episode.active ? flightEnd : episode.activated_at);
  return timestamp <= end;
}

function activeEpisodesAt(
  episodes: NormalizedAlertEpisode[],
  timestamp: number,
  flightEnd: number,
): NormalizedAlertEpisode[] {
  return episodes.filter((episode) => episodeActiveAt(episode, timestamp, flightEnd));
}

function worstSeverity(episodes: NormalizedAlertEpisode[]): NormalizedAlertLevel {
  if (episodes.some((ep) => normalizeAlertRule(ep.rule) === 'alert')) return 'alert';
  if (episodes.some((ep) => normalizeAlertRule(ep.rule) === 'warn')) return 'warn';
  return 'info';
}

function primaryEpisode(episodes: NormalizedAlertEpisode[]): NormalizedAlertEpisode {
  const sorted = [...episodes].sort((a, b) => {
    const rank = (rule?: string) => {
      const norm = normalizeAlertRule(rule);
      if (norm === 'alert') return 0;
      if (norm === 'warn') return 1;
      return 2;
    };
    return rank(a.rule) - rank(b.rule);
  });
  return sorted[0];
}

export function buildAlertPathSegments(
  telemetry: TelemetryPoint[],
  alerts: Alert[],
  flightEndTime?: number,
): AlertPathSegment[] {
  const points = telemetry
    .filter((point) => point.latitude != null && point.longitude != null)
    .sort((a, b) => a.timestamp - b.timestamp);
  if (points.length < 2) return [];

  const episodes = normalizeAlertEpisodes(alerts);
  if (!episodes.length) return [];

  const flightEnd = flightEndTime ?? points[points.length - 1].timestamp;
  const segments: AlertPathSegment[] = [];
  let current: AlertPathSegment | null = null;

  for (let i = 0; i < points.length - 1; i += 1) {
    const start = points[i];
    const end = points[i + 1];
    const midTime = (start.timestamp + end.timestamp) / 2;
    const active = activeEpisodesAt(episodes, midTime, flightEnd);
    const startCoord: [number, number] = [start.latitude!, start.longitude!];
    const endCoord: [number, number] = [end.latitude!, end.longitude!];

    if (!active.length) {
      current = null;
      continue;
    }

    const severity = worstSeverity(active);
    const primary = primaryEpisode(active);
    const sameSegment =
      current &&
      current.severity === severity &&
      current.rule === primary.rule &&
      current.zone === primary.zone;

    if (sameSegment && current) {
      current.latlngs.push(endCoord);
      continue;
    }

    current = {
      latlngs: [startCoord, endCoord],
      severity,
      rule: primary.rule,
      zone: primary.zone,
    };
    segments.push(current);
  }

  return segments;
}
