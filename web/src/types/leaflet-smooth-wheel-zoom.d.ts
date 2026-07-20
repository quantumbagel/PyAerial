/**
 * Type augmentation for @luomus/leaflet-smooth-wheel-zoom
 * The package ships no TypeScript declarations, so we extend Leaflet's
 * MapOptions here to keep the rest of the codebase type-safe.
 */
declare module 'leaflet' {
  interface MapOptions {
    smoothWheelZoom?: boolean | 'center';
    smoothSensitivity?: number;
  }
}
