import { ZONE_PALETTE, parseHex } from './colors';

interface ZoneColorSource {
  name: string;
  color?: string;
  rules?: { name: string; color?: string }[];
}

export interface ColorPair {
  stroke: string;
  fill: string;
}

function asColorPair(hex: string): ColorPair {
  return { stroke: hex, fill: hex };
}

function normalize(name: string | null | undefined): string {
  return (name || '').trim().toLowerCase();
}

function findZone(zoneName: string, zones?: ZoneColorSource[]) {
  const key = normalize(zoneName);
  return zones?.find((z) => normalize(z.name) === key);
}

function zoneColorIndex(zoneName: string, zones?: ZoneColorSource[]): number {
  const key = normalize(zoneName);
  const idx = zones?.findIndex((z) => normalize(z.name) === key) ?? -1;
  if (idx >= 0) return idx;
  let hash = 0;
  for (let i = 0; i < zoneName.length; i++) {
    hash = zoneName.charCodeAt(i) + ((hash << 5) - hash);
  }
  return Math.abs(hash);
}

/** A zone's configured colour, else a stable palette pick. */
export function zoneColorFor(zoneName: string, zones?: ZoneColorSource[]): ColorPair {
  const zone = findZone(zoneName, zones);
  if (zone?.color) return asColorPair(zone.color);
  return asColorPair(ZONE_PALETTE[zoneColorIndex(zoneName, zones) % ZONE_PALETTE.length]);
}

/**
 * Colour for one alert: the rule's colour inside its zone, else the global
 * `alert_colors[rule]`, else the zone's colour.
 */
export function alertColorFor(
  zoneName: string,
  ruleName?: string | null,
  zones?: ZoneColorSource[],
  alertColors?: Record<string, string>,
): ColorPair {
  const rule = (ruleName || '').trim();
  const zone = findZone(zoneName, zones);
  if (rule && zone?.rules?.length) {
    const zoneRule = zone.rules.find((r) => normalize(r.name) === rule.toLowerCase());
    if (zoneRule?.color) return asColorPair(zoneRule.color);
  }
  if (rule && alertColors) {
    const configured = alertColors[rule] ?? alertColors[rule.toLowerCase()];
    if (configured) return asColorPair(configured);
  }
  return zoneColorFor(zoneName, zones);
}

/** Lighten dark/red zone colours so badge text stays readable on dark panels. */
export function getAccessibleBadgeTextColor(colorHex: string): string {
  const rgb = parseHex(colorHex);
  if (!rgb) return colorHex;
  const [r, g, b] = rgb;

  // Reds read poorly on dark backgrounds; use a light red instead.
  if (r > g * 1.5 && r > b * 1.5 && Math.abs(g - b) < 60) {
    return '#fca5a5';
  }

  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  if (luminance < 0.55) {
    const factor = 0.55 / Math.max(luminance, 0.1);
    const lift = (c: number) => Math.min(255, Math.round(c * factor + 60));
    return `#${[r, g, b].map((c) => lift(c).toString(16).padStart(2, '0')).join('')}`;
  }

  return colorHex;
}
