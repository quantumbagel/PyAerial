/**
 * The vector basemap. Loaded with a dynamic import so MapLibre (the bulk of
 * the bundle) downloads in parallel with, not before, the app's first render.
 */
import type * as L from 'leaflet';
import { maplibreGL } from '@maplibre/maplibre-gl-leaflet';
import { setWorkerUrl } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import 'maplibre-gl/dist/maplibre-gl.css';

setWorkerUrl(workerUrl);

const OPENFREEMAP_DARK = 'https://tiles.openfreemap.org/styles/dark';

export function addBasemap(map: L.Map): L.Layer {
  return maplibreGL({ style: OPENFREEMAP_DARK }).addTo(map);
}
