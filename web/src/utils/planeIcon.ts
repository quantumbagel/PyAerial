import * as L from 'leaflet';
import type { ActiveAlert } from '../api/types';
import { COLOR_HEX } from './colors';
import { flightAlertSeverity } from './format';

export function createPlaneIcon(
  heading: number | null | undefined,
  isSelected: boolean,
  isLive: boolean,
  activeAlerts?: ActiveAlert[] | null,
): L.DivIcon {
  const severity = flightAlertSeverity(activeAlerts);
  let fill: string;
  let stroke: string;
  let size: number;
  let opacity: number;
  let extraStyle = '';

  if (severity === 'alert') {
    fill = COLOR_HEX.alert;
    stroke = COLOR_HEX.alertDark;
    size = isSelected ? 30 : 28;
    opacity = 1.0;
    if (isSelected) {
      extraStyle = `filter: drop-shadow(0 0 4px ${COLOR_HEX.alert}) drop-shadow(0 0 8px ${COLOR_HEX.alert}80);`;
    }
  } else if (isSelected) {
    fill = '#ffffff';
    stroke = COLOR_HEX.accent;
    size = 30;
    opacity = 1.0;
    extraStyle = `filter: drop-shadow(0 0 4px ${COLOR_HEX.accent}) drop-shadow(0 0 10px ${COLOR_HEX.accent}8c);`;
  } else if (severity === 'warn' || (severity === 'info' && activeAlerts?.length)) {
    fill = COLOR_HEX.warn;
    stroke = COLOR_HEX.warnDark;
    size = 26;
    opacity = 1.0;
  } else if (isLive) {
    fill = COLOR_HEX.live;
    stroke = COLOR_HEX.liveDark;
    size = 24;
    opacity = 1.0;
  } else {
    fill = COLOR_HEX.default;
    stroke = COLOR_HEX.defaultDark;
    size = 24;
    opacity = 0.65;
  }

  const svg = `
    <svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${fill}" stroke="${stroke}" stroke-width="1" xmlns="http://www.w3.org/2000/svg" style="transform: rotate(${heading || 0}deg); transform-origin: center; opacity: ${opacity}; transition: transform 0.2s; ${extraStyle}">
      <path d="M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L14 19v-5.5L21 16z"/>
    </svg>
  `;
  return L.divIcon({
    html: svg,
    className: 'plane-icon-div',
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

export function pathStyleForFlight(
  flight: { active_alerts?: ActiveAlert[] } | undefined,
  isSelected: boolean,
): L.PolylineOptions {
  const severity = flightAlertSeverity(flight?.active_alerts);
  let color: string = COLOR_HEX.default;
  if (severity === 'alert') color = COLOR_HEX.alert;
  else if (severity === 'warn' || (severity === 'info' && flight?.active_alerts?.length)) {
    color = COLOR_HEX.warn;
  }
  if (isSelected) color = COLOR_HEX.accent;
  return {
    color,
    weight: isSelected ? 3 : 2,
    opacity: isSelected ? 0.85 : 0.4,
  };
}
