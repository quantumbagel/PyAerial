import L from 'leaflet';

export function createPlaneIcon(
  heading: number | null | undefined,
  isSelected: boolean,
  isLive: boolean,
  level?: string,
): L.DivIcon {
  const alertLevel = (level || '').toLowerCase();
  let fill: string;
  let stroke: string;
  let size: number;
  let opacity: number;
  let extraStyle = '';

  if (alertLevel === 'alert') {
    fill = '#ef4444';
    stroke = '#7f1d1d';
    size = isSelected ? 30 : 28;
    opacity = 1.0;
    if (isSelected) {
      extraStyle = 'filter: drop-shadow(0 0 4px #ef4444) drop-shadow(0 0 8px rgba(239, 68, 68, 0.5));';
    }
  } else if (isSelected) {
    fill = '#ffffff';
    stroke = '#3b82f6';
    size = 30;
    opacity = 1.0;
    extraStyle = 'filter: drop-shadow(0 0 4px #3b82f6) drop-shadow(0 0 10px rgba(59, 130, 246, 0.55));';
  } else if (alertLevel === 'warn') {
    fill = '#f59e0b';
    stroke = '#78350f';
    size = 26;
    opacity = 1.0;
  } else if (isLive) {
    fill = '#34d399';
    stroke = '#047857';
    size = 24;
    opacity = 1.0;
  } else {
    fill = '#64748b';
    stroke = '#334155';
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

export const ZONE_COLORS = [
  { stroke: '#f59e0b', fill: '#f59e0b' },
  { stroke: '#3b82f6', fill: '#3b82f6' },
  { stroke: '#a855f7', fill: '#a855f7' },
  { stroke: '#14b8a6', fill: '#14b8a6' },
];

export function pathStyleForFlight(
  flight: { level?: string } | undefined,
  isSelected: boolean,
): L.PolylineOptions {
  const alertLevel = (flight?.level || '').toLowerCase();
  let color = '#64748b';
  if (alertLevel === 'alert') color = '#ef4444';
  else if (alertLevel === 'warn') color = '#f59e0b';
  if (isSelected) color = '#3b82f6';
  return {
    color,
    weight: isSelected ? 3 : 2,
    opacity: isSelected ? 0.85 : 0.4,
  };
}
