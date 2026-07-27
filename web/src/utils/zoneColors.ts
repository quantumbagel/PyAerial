export const ZONE_COLORS = [
  { stroke: '#f59e0b', fill: '#f59e0b' },
  { stroke: '#3b82f6', fill: '#3b82f6' },
  { stroke: '#a855f7', fill: '#a855f7' },
  { stroke: '#14b8a6', fill: '#14b8a6' },
];

export interface ZoneColorSource {
  name: string;
  color?: string;
  rules?: { name: string; color?: string }[];
}

function asColorPair(hex: string) {
  return { stroke: hex, fill: hex };
}

export function zoneColorIndex(zoneName: string, zones?: ZoneColorSource[]): number {
  const normalized = zoneName.trim().toLowerCase();
  if (zones?.length) {
    const idx = zones.findIndex((z) => z.name.trim().toLowerCase() === normalized);
    if (idx >= 0) return idx;
  }
  let hash = 0;
  for (let i = 0; i < zoneName.length; i++) {
    hash = zoneName.charCodeAt(i) + ((hash << 5) - hash);
  }
  return Math.abs(hash);
}

export function zoneColorFor(zoneName: string, zones?: ZoneColorSource[]) {
  const normalized = zoneName.trim().toLowerCase();
  const zone = zones?.find((z) => z.name.trim().toLowerCase() === normalized);
  if (zone?.color) {
    return asColorPair(zone.color);
  }
  const idx = zoneColorIndex(zoneName, zones);
  return ZONE_COLORS[idx % ZONE_COLORS.length];
}

export function alertColorFor(
  zoneName: string,
  ruleName?: string,
  zones?: ZoneColorSource[],
  alertColors?: Record<string, string>,
) {
  const rule = (ruleName || '').trim();
  const normalizedZone = zoneName.trim().toLowerCase();
  const zone = zones?.find((z) => z.name.trim().toLowerCase() === normalizedZone);

  if (rule && zone?.rules?.length) {
    const zoneRule = zone.rules.find((r) => r.name.trim().toLowerCase() === rule.toLowerCase());
    if (zoneRule?.color) {
      return asColorPair(zoneRule.color);
    }
  }

  if (rule && alertColors) {
    const configured = alertColors[rule] ?? alertColors[rule.toLowerCase()];
    if (configured) {
      return asColorPair(configured);
    }
  }

  return zoneColorFor(zoneName, zones);
}

function parseHex(hex: string): [number, number, number] | null {
  const clean = hex.replace('#', '').trim();
  if (clean.length === 3) {
    const r = parseInt(clean[0] + clean[0], 16);
    const g = parseInt(clean[1] + clean[1], 16);
    const b = parseInt(clean[2] + clean[2], 16);
    return [r, g, b];
  }
  if (clean.length === 6) {
    const r = parseInt(clean.slice(0, 2), 16);
    const g = parseInt(clean.slice(2, 4), 16);
    const b = parseInt(clean.slice(4, 6), 16);
    return [r, g, b];
  }
  return null;
}

export function getAccessibleBadgeTextColor(colorHex: string): string {
  const rgb = parseHex(colorHex);
  if (!rgb) return colorHex;
  const [r, g, b] = rgb;

  // Red colors (such as #ef4444, #7f1d1d, #b91c1c) offer poor contrast against dark backgrounds.
  // Map red tones (where red dominates and green/blue are roughly equal) to a light, high-contrast red (#fca5a5).
  if (r > g * 1.5 && r > b * 1.5 && Math.abs(g - b) < 60) {
    return '#fca5a5';
  }

  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  if (luminance < 0.55) {
    const factor = 0.55 / Math.max(luminance, 0.1);
    const newR = Math.min(255, Math.round(r * factor + 60));
    const newG = Math.min(255, Math.round(g * factor + 60));
    const newB = Math.min(255, Math.round(b * factor + 60));
    return `#${newR.toString(16).padStart(2, '0')}${newG.toString(16).padStart(2, '0')}${newB.toString(16).padStart(2, '0')}`;
  }

  return colorHex;
}

