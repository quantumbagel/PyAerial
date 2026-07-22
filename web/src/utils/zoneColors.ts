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
