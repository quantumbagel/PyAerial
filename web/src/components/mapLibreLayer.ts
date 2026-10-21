/**
 * The vector basemap. Loaded with a dynamic import so MapLibre (the bulk of
 * the bundle) downloads in parallel with, not before, the app's first render.
 */
import type * as L from 'leaflet';
import { setWorkerUrl } from 'maplibre-gl';
import { maplibreGL } from '@maplibre/maplibre-gl-leaflet';
import 'maplibre-gl/dist/maplibre-gl.css';
// MapLibre 6 otherwise derives `./maplibre-gl-worker.mjs` from its own module
// URL at runtime, a file Vite never emits (404 => no tiles). `?worker&url`
// makes Vite bundle the worker (including its shared chunk) and hand us the
// emitted URL.
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';

const OPENFREEMAP_DARK = 'https://tiles.openfreemap.org/styles/dark';

setWorkerUrl(workerUrl);

export function addBasemap(map: L.Map): L.Layer {
  return maplibreGL({ style: OPENFREEMAP_DARK }).addTo(map);
}
