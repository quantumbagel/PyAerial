import * as L from 'leaflet';
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

function rotation(heading: number | null | undefined): string {
  return `rotate(${Number.isFinite(heading) ? heading : 0}deg)`;
}

const PLANE_PATH =
  'M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L14 19v-5.5L21 16z';

/**
 * Build a plane marker icon. Heading changes afterwards should go through
 * setPlaneHeading so the existing element rotates (and animates) in place.
 */
export function createPlaneIcon(style: PlaneStyle, heading: number | null | undefined): L.DivIcon {
  const svgNs = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNs, 'svg');
  svg.setAttribute('class', 'plane-icon__svg');
  svg.setAttribute('width', String(style.size));
  svg.setAttribute('height', String(style.size));
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', style.fill);
  svg.setAttribute('stroke', style.stroke);
  svg.setAttribute('stroke-width', '1');
  svg.setAttribute('aria-hidden', 'true');
  svg.style.opacity = String(style.opacity);
  svg.style.transform = rotation(heading);
  if (style.glow) {
    svg.style.filter = `drop-shadow(0 0 4px ${style.glow}) drop-shadow(0 0 9px color-mix(in srgb, ${style.glow} 55%, transparent))`;
  }
  const path = document.createElementNS(svgNs, 'path');
  path.setAttribute('d', PLANE_PATH);
  svg.appendChild(path);
  const wrap = document.createElement('span');
  wrap.className = 'plane-icon__wrap';
  wrap.appendChild(svg);
  return L.divIcon({
    html: wrap,
    className: 'plane-icon',
    iconSize: [style.size, style.size],
    iconAnchor: [style.size / 2, style.size / 2],
  });
}

/** Rotate an existing plane marker without replacing its DOM (keeps the CSS transition). */
export function setPlaneHeading(marker: L.Marker, heading: number | null | undefined): void {
  const svg = marker.getElement()?.querySelector<SVGElement>('.plane-icon__svg');
  if (svg) svg.style.transform = rotation(heading);
}

export function pathStyleForFlight(
  flight: PlaneLike | undefined,
  isSelected: boolean,
  zones?: Zone[],
  alertColors?: Record<string, string>,
): L.PolylineOptions {
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
