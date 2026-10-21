import type { Alert, TelemetryPoint } from '../api/types';
import { alertSeverityRank, dedupeAlerts, isAlertActive } from './alertData';

export interface AlertPathSegment {
  latlngs: [number, number][];
  rule?: string;
  zone?: string;
}

interface Episode {
  zone?: string;
  rule?: string;
  start: number;
  end: number;
  rank: number;
}

function toEpisodes(alerts: Alert[], flightEnd: number): Episode[] {
  return dedupeAlerts(alerts).map((alert) => {
    const start = alert.activated_at ?? 0;
    // Active episodes run to the end of the flight; ended ones without a
    // timestamp collapse to their start.
    const end = isAlertActive(alert) ? flightEnd : (alert.deactivated_at ?? start);
    return { zone: alert.zone, rule: alert.rule, start, end, rank: alertSeverityRank(alert.rule) };
  });
}

/** The highest-severity episode active at `t` (earliest-started wins ties). */
function primaryAt(episodes: Episode[], t: number): Episode | null {
  let best: Episode | null = null;
  for (const ep of episodes) {
    if (t < ep.start || t > ep.end) continue;
    if (!best || ep.rank > best.rank || (ep.rank === best.rank && ep.start < best.start)) {
      best = ep;
    }
  }
  return best;
}

/**
 * Split a flight path into the stretches flown while an alert was active,
 * each tagged with the alert that should colour it.
 */
export function buildAlertPathSegments(
  telemetry: TelemetryPoint[],
  alerts: Alert[],
  flightEndTime?: number,
): AlertPathSegment[] {
  const points = telemetry.filter((p) => p.latitude != null && p.longitude != null);
  if (points.length < 2 || !alerts.length) return [];
  // Callers keep telemetry ordered; only sort when they didn't.
  for (let i = 1; i < points.length; i += 1) {
    if (points[i].timestamp < points[i - 1].timestamp) {
      points.sort((a, b) => a.timestamp - b.timestamp);
      break;
    }
  }

  const flightEnd = flightEndTime ?? points[points.length - 1].timestamp;
  const episodes = toEpisodes(alerts, flightEnd);
  if (!episodes.length) return [];

  const segments: AlertPathSegment[] = [];
  let current: AlertPathSegment | null = null;
  for (let i = 0; i < points.length - 1; i += 1) {
    const start = points[i];
    const end = points[i + 1];
    const primary = primaryAt(episodes, (start.timestamp + end.timestamp) / 2);
    if (!primary) {
      current = null;
      continue;
    }
    const endCoord: [number, number] = [end.latitude!, end.longitude!];
    if (current && current.rule === primary.rule && current.zone === primary.zone) {
      current.latlngs.push(endCoord);
      continue;
    }
    current = {
      latlngs: [[start.latitude!, start.longitude!], endCoord],
      rule: primary.rule,
      zone: primary.zone,
    };
    segments.push(current);
  }
  return segments;
}
