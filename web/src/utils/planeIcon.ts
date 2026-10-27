import type { ActiveAlert, Integrity, Zone } from '../api/types';
import { alertVisual, primaryAlert } from './alertData';
import { PALETTE } from './colors';

export interface PlaneStyle {
  size: number;
  fill: string;
  stroke: string;
  opacity: number;
  glow: string | null;
}

export interface PathStyle {
  color: string;
  weight: number;
  opacity: number;
}

export interface PlaneImage {
  width: number;
  height: number;
  data: Uint8ClampedArray;
}

interface PlaneLike {
  is_live?: boolean;
  active_alerts?: ActiveAlert[] | null;
  integrity?: Integrity | null;
}

/** Marker look for a flight; alerting planes use their alert's configured colour. */
export function planeStyle(
  flight: PlaneLike,
  isSelected: boolean,
  zones?: Zone[],
  alertColors?: Record<string, string>,
): PlaneStyle {
  const primary = primaryAlert(flight.active_alerts);
  if (primary) {
    const visual = alertVisual(primary, zones, alertColors);
    return {
      size: isSelected ? 30 : 28,
      fill: visual.color,
      stroke: visual.dark,
      opacity: 1,
      glow: isSelected ? visual.color : null,
    };
  }
  // Flagged tracks keep the live size but read as "not trusted": dark fill,
  // status-coloured outline.
  const status = flight.integrity?.status;
  if (status === 'spoofed' || status === 'suspect') {
    const tone = status === 'spoofed' ? PALETTE.alert : PALETTE.warn;
    return {
      size: isSelected ? 30 : 24,
      fill: status === 'spoofed' ? PALETTE.alertDark : PALETTE.warnDark,
      stroke: tone,
      opacity: 1,
      glow: isSelected ? tone : null,
    };
  }
  if (isSelected) {
    return { size: 30, fill: PALETTE.white, stroke: PALETTE.accent, opacity: 1, glow: PALETTE.accent };
  }
  if (flight.is_live) {
    return { size: 24, fill: PALETTE.live, stroke: PALETTE.liveDark, opacity: 1, glow: null };
  }
  return { size: 24, fill: PALETTE.neutral, stroke: PALETTE.neutralDark, opacity: 0.65, glow: null };
}

export function planeStyleKey(style: PlaneStyle): string {
  return `${style.size}|${style.fill}|${style.stroke}|${style.opacity}|${style.glow ?? ''}`;
}

/** Sprite id for a plane style. Heading is applied with `icon-rotate`, not the image. */
export function planeIconId(style: PlaneStyle): string {
  return `plane-${planeStyleKey(style).replace(/[^a-zA-Z0-9-]/g, '_')}`;
}

export function planeHeading(heading: number | null | undefined): number {
  return Number.isFinite(heading) ? (heading as number) : 0;
}

const PLANE_PATH =
  'M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L14 19v-5.5L21 16z';

function emptyImage(width: number, height: number): PlaneImage {
  return { width, height, data: new Uint8ClampedArray(width * height * 4) };
}

/**
 * Rasterise a plane icon for MapLibre `addImage`. Heading is not baked in:
 * the symbol layer rotates the same sprite with `icon-rotate`.
 */
export function renderPlaneIcon(style: PlaneStyle, pixelRatio = 1): PlaneImage {
  const pad = style.glow ? 6 : 1;
  const cssSize = Math.max(1, Math.round(style.size));
  const width = Math.max(1, Math.round((cssSize + pad * 2) * pixelRatio));
  const height = width;
  if (typeof document === 'undefined' || typeof Path2D === 'undefined') {
    return emptyImage(width, height);
  }
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) return emptyImage(width, height);

  ctx.translate(pad * pixelRatio, pad * pixelRatio);
  const scale = (cssSize / 24) * pixelRatio;
  ctx.scale(scale, scale);
  if (style.glow) {
    ctx.shadowColor = style.glow;
    ctx.shadowBlur = 8 / scale;
  }
  ctx.globalAlpha = style.opacity;
  ctx.fillStyle = style.fill;
  ctx.strokeStyle = style.stroke;
  ctx.lineWidth = 1;
  ctx.lineJoin = 'round';
  const path = new Path2D(PLANE_PATH);
  ctx.fill(path);
  ctx.stroke(path);
  try {
    return ctx.getImageData(0, 0, width, height);
  } catch {
    return emptyImage(width, height);
  }
}

export function pathStyleForFlight(
  flight: PlaneLike | undefined,
  isSelected: boolean,
  zones?: Zone[],
  alertColors?: Record<string, string>,
): PathStyle {
  const primary = primaryAlert(flight?.active_alerts);
  let color: string = PALETTE.neutral;
  if (isSelected) color = PALETTE.accent;
  else if (primary) color = alertVisual(primary, zones, alertColors).color;
  return {
    color,
    weight: isSelected ? 3 : 2,
    opacity: isSelected ? 0.85 : 0.4,
  };
}
