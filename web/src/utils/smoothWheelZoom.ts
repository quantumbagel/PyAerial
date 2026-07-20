/**
 * Leaflet SmoothWheelZoom plugin – TypeScript port
 * Original: https://github.com/mutsuyuki/Leaflet.SmoothWheelZoom
 *
 * Provides Google Maps–style smooth pinch-to-zoom centred on the cursor.
 */

import L from 'leaflet';

// ── augment Leaflet's Map options type ────────────────────────────────────────
declare module 'leaflet' {
  interface MapOptions {
    smoothWheelZoom?: boolean | 'center';
    smoothSensitivity?: number;
  }
}

// ── handler ───────────────────────────────────────────────────────────────────
const SmoothWheelZoom = L.Handler.extend({

  addHooks(this: any) {
    L.DomEvent.on(this._map._container, 'wheel', this._onWheelScroll, this);
  },

  removeHooks(this: any) {
    L.DomEvent.off(this._map._container, 'wheel', this._onWheelScroll, this);
  },

  // ── event entry-point ──────────────────────────────────────────────────────
  _onWheelScroll(this: any, e: WheelEvent) {
    if (!this._isWheeling) {
      this._onWheelStart(e);
    }
    this._onWheeling(e);
  },

  // ── initialise a new wheel gesture ────────────────────────────────────────
  _onWheelStart(this: any, e: WheelEvent) {
    const map: L.Map = this._map;
    this._isWheeling   = true;
    this._wheelMousePosition = map.mouseEventToContainerPoint(e);
    this._centerPoint        = map.getSize().divideBy(2);
    this._startLatLng        = map.containerPointToLatLng(this._centerPoint);
    this._wheelMouseLatLng   = map.containerPointToLatLng(this._wheelMousePosition);
    this._startZoom          = map.getZoom();
    this._moved              = false;
    this._zooming            = true;

    (map as any)._stop(); // internal Leaflet method – no public equivalent
    if ((map as any)._panAnim) (map as any)._panAnim.stop();

    this._goalZoom     = map.getZoom();
    this._prevCenter   = map.getCenter();
    this._prevZoom     = map.getZoom();
  },

  // ── accumulate delta each wheel tick ──────────────────────────────────────
  _onWheeling(this: any, e: WheelEvent) {
    const map: L.Map    = this._map;
    const options       = map.options as L.MapOptions;
    const sensitivity   = options.smoothSensitivity ?? 1;

    // Normalise delta: pixels → zoom-levels
    let delta = L.DomEvent.getWheelDelta(e);
    delta = Math.max(Math.min(delta, 4), -4);          // clamp per event
    this._goalZoom = this._goalZoom + (delta * 0.003 * sensitivity);
    this._goalZoom = Math.max(
      Math.min(this._goalZoom, map.getMaxZoom()),
      map.getMinZoom(),
    );

    L.DomEvent.stop(e);

    // Cancel any pending end-of-wheel timer
    if (this._wheelTimeout) {
      clearTimeout(this._wheelTimeout);
    }

    // Schedule the frame if not already running
    if (!this._rafId) {
      this._rafId = requestAnimationFrame(() => this._animate());
    }

    // Detect end of wheel gesture
    this._wheelTimeout = window.setTimeout(() => this._onWheelEnd(), 200);
  },

  // ── rAF animation loop ────────────────────────────────────────────────────
  _animate(this: any) {
    this._rafId = null;
    const map: L.Map = this._map;

    const currentZoom = map.getZoom();
    const goalZoom    = this._goalZoom;

    if (Math.abs(currentZoom - goalZoom) < 0.001) {
      // Close enough – snap and stop
      map.setZoom(goalZoom, { animate: false });
      return;
    }

    // Lerp toward goal
    const newZoom = currentZoom + (goalZoom - currentZoom) * 0.12;

    // Determine the pivot point (mouse or centre)
    const options = map.options as L.MapOptions;
    let pivot: L.Point;
    if (options.smoothWheelZoom === 'center') {
      pivot = this._centerPoint;
    } else {
      pivot = this._wheelMousePosition;
    }

    // Calculate the new centre so the point under the cursor stays fixed
    const scale = map.getZoomScale(newZoom, currentZoom);
    const viewHalf = map.getSize().divideBy(2);

    const newCenter = map
      .project(this._wheelMouseLatLng, newZoom)
      .subtract(pivot)
      .add(viewHalf)
      .add(viewHalf.subtract(pivot).multiplyBy(-(1 / scale - 1)));

    const newLatLng = map.unproject(newCenter, newZoom);

    map.setView(newLatLng, newZoom, { animate: false });

    if (this._zooming) {
      this._rafId = requestAnimationFrame(() => this._animate());
    }
  },

  // ── finish gesture ────────────────────────────────────────────────────────
  _onWheelEnd(this: any) {
    this._isWheeling = false;
    this._zooming    = false;
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
  },
});

// ── register handler & disable default scroll-zoom ────────────────────────────
export function initSmoothWheelZoom(map: L.Map): void {
  // Disable Leaflet's built-in scroll zoom
  map.scrollWheelZoom.disable();

  // Add our smooth handler
  (L.Map as any).addInitHook('addHandler', 'smoothWheelZoom', SmoothWheelZoom);

  // If not already added via initHook (map already created), attach manually
  if (!(map as any).smoothWheelZoom) {
    (map as any).smoothWheelZoom = new (SmoothWheelZoom as any)(map);
  }
  (map as any).smoothWheelZoom.enable();
}
