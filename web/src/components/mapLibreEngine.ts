/**
 * MapLibre GL bootstrap. Loaded with a dynamic import so MapLibre (the bulk of
 * the bundle) downloads in parallel with, not before, the app's first render.
 */
import { Map, Popup, setWorkerUrl } from 'maplibre-gl';
import type { GeoJSONSource } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
// MapLibre 6 otherwise derives `./maplibre-gl-worker.mjs` from its own module
// URL at runtime, a file Vite never emits (404 => no tiles). `?worker&url`
// makes Vite bundle the worker (including its shared chunk) and hand us the
// emitted URL.
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import { PALETTE } from '../utils/colors';

setWorkerUrl(workerUrl);

export { Map, Popup };
export type { GeoJSONSource };

export const OPENFREEMAP_DARK = 'https://tiles.openfreemap.org/styles/dark';
export const DEFAULT_CENTER: [number, number] = [-78.696, 35.727];
export const DEFAULT_ZOOM = 8;

export const SRC = {
  planes: 'pya-planes',
  paths: 'pya-paths',
  pathAlerts: 'pya-path-alerts',
  zones: 'pya-zones',
  stations: 'pya-stations',
  telemetry: 'pya-telemetry',
  emitter: 'pya-emitter',
  emitterLine: 'pya-emitter-line',
} as const;

export const LYR = {
  zonesFill: 'pya-zones-fill',
  zonesLine: 'pya-zones-line',
  zonesLabel: 'pya-zones-label',
  pathsHit: 'pya-paths-hit',
  paths: 'pya-paths',
  pathAlertsHit: 'pya-path-alerts-hit',
  pathAlerts: 'pya-path-alerts',
  emitterLine: 'pya-emitter-line',
  home: 'pya-home',
  receivers: 'pya-receivers',
  telemetry: 'pya-telemetry',
  emitter: 'pya-emitter',
  planes: 'pya-planes',
} as const;

export const HOVER_LAYERS = [
  LYR.planes,
  LYR.paths,
  LYR.pathAlerts,
  LYR.home,
  LYR.receivers,
  LYR.telemetry,
  LYR.emitter,
] as const;

export const SELECT_LAYERS = [LYR.planes, LYR.pathAlertsHit, LYR.pathAlerts, LYR.pathsHit, LYR.paths];

export const EMPTY_FC: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] };

const ZONE_FONT = ['Noto Sans Regular'];

export function createMap(container: HTMLElement): Map {
  const map = new Map({
    container,
    style: OPENFREEMAP_DARK,
    center: DEFAULT_CENTER,
    zoom: DEFAULT_ZOOM,
    minZoom: 1,
    maxPitch: 0,
    dragRotate: false,
    touchPitch: false,
    pitch: 0,
    attributionControl: { compact: true },
    maplibreLogo: false,
    fadeDuration: 0,
  });
  map.touchZoomRotate.disableRotation();
  return map;
}

export function setSourceData(map: Map, sourceId: string, data: GeoJSON.GeoJSON): void {
  const source = map.getSource(sourceId);
  if (source && 'setData' in source) {
    (source as GeoJSONSource).setData(data);
  }
}

function emptySource(): { type: 'geojson'; data: GeoJSON.FeatureCollection } {
  return { type: 'geojson', data: EMPTY_FC };
}

/** Overlay sources and layers on top of the vector style. Call after `load`. */
export function addOverlayLayers(map: Map): void {
  map.addSource(SRC.zones, emptySource());
  map.addSource(SRC.paths, emptySource());
  map.addSource(SRC.pathAlerts, emptySource());
  map.addSource(SRC.emitterLine, emptySource());
  map.addSource(SRC.stations, emptySource());
  map.addSource(SRC.telemetry, emptySource());
  map.addSource(SRC.emitter, emptySource());
  map.addSource(SRC.planes, emptySource());

  map.addLayer({
    id: LYR.zonesFill,
    type: 'fill',
    source: SRC.zones,
    paint: {
      'fill-color': ['get', 'fill'],
      'fill-opacity': 0.14,
    },
  });
  map.addLayer({
    id: LYR.zonesLine,
    type: 'line',
    source: SRC.zones,
    paint: {
      'line-color': ['get', 'stroke'],
      'line-width': 2,
      'line-opacity': 0.9,
    },
  });
  map.addLayer({
    id: LYR.zonesLabel,
    type: 'symbol',
    source: SRC.zones,
    layout: {
      'text-field': ['upcase', ['get', 'name']],
      'text-font': ZONE_FONT,
      'text-size': 12,
      'text-letter-spacing': 0.06,
      'text-anchor': 'center',
      'text-allow-overlap': true,
      'text-ignore-placement': true,
      'text-max-width': 14,
    },
    paint: {
      'text-color': '#a1a1aa',
      'text-halo-color': 'rgba(26, 26, 26, 0.85)',
      'text-halo-width': 1.4,
    },
  });

  map.addLayer({
    id: LYR.pathsHit,
    type: 'line',
    source: SRC.paths,
    paint: {
      'line-color': '#000000',
      'line-width': ['+', ['get', 'weight'], 10],
      'line-opacity': 0,
    },
  });
  map.addLayer({
    id: LYR.paths,
    type: 'line',
    source: SRC.paths,
    layout: {
      'line-join': 'round',
      'line-cap': 'round',
    },
    paint: {
      'line-color': ['get', 'color'],
      'line-width': ['get', 'weight'],
      'line-opacity': ['get', 'opacity'],
    },
  });
  map.addLayer({
    id: LYR.pathAlertsHit,
    type: 'line',
    source: SRC.pathAlerts,
    paint: {
      'line-color': '#000000',
      'line-width': ['+', ['get', 'weight'], 10],
      'line-opacity': 0,
    },
  });
  map.addLayer({
    id: LYR.pathAlerts,
    type: 'line',
    source: SRC.pathAlerts,
    layout: {
      'line-join': 'round',
      'line-cap': 'round',
    },
    paint: {
      'line-color': ['get', 'color'],
      'line-width': ['get', 'weight'],
      'line-opacity': 0.95,
    },
  });
  map.addLayer({
    id: LYR.emitterLine,
    type: 'line',
    source: SRC.emitterLine,
    paint: {
      'line-color': PALETTE.alert,
      'line-width': 1.5,
      'line-opacity': 0.8,
      'line-dasharray': [2, 4],
    },
  });

  map.addLayer({
    id: LYR.home,
    type: 'circle',
    source: SRC.stations,
    filter: ['==', ['get', 'kind'], 'home'],
    paint: {
      'circle-radius': 6,
      'circle-color': PALETTE.home,
      'circle-opacity': 0.95,
      'circle-stroke-width': 2,
      'circle-stroke-color': PALETTE.home,
    },
  });
  map.addLayer({
    id: LYR.receivers,
    type: 'circle',
    source: SRC.stations,
    filter: ['==', ['get', 'kind'], 'receiver'],
    paint: {
      'circle-radius': 5,
      'circle-color': PALETTE.home,
      'circle-opacity': 0.2,
      'circle-stroke-width': 2,
      'circle-stroke-color': PALETTE.home,
    },
  });
  map.addLayer({
    id: LYR.telemetry,
    type: 'circle',
    source: SRC.telemetry,
    paint: {
      'circle-radius': 7,
      'circle-color': PALETTE.accent,
      'circle-opacity': 1,
      'circle-stroke-width': 2,
      'circle-stroke-color': PALETTE.white,
    },
  });
  map.addLayer({
    id: LYR.emitter,
    type: 'circle',
    source: SRC.emitter,
    paint: {
      'circle-radius': 7,
      'circle-color': PALETTE.alertDark,
      'circle-opacity': 0.9,
      'circle-stroke-width': 2,
      'circle-stroke-color': PALETTE.alert,
    },
  });

  map.addLayer({
    id: LYR.planes,
    type: 'symbol',
    source: SRC.planes,
    layout: {
      'icon-image': ['get', 'icon'],
      'icon-rotate': ['coalesce', ['get', 'heading'], 0],
      'icon-rotation-alignment': 'map',
      'icon-pitch-alignment': 'map',
      'icon-allow-overlap': true,
      'icon-ignore-placement': true,
      'icon-anchor': 'center',
      'symbol-sort-key': ['get', 'z'],
    },
  });
}
