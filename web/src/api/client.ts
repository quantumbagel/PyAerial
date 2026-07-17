import type { Alert, FlightDetail, FlightSummary, PortalView, TelemetryPoint, ZonesData } from './types';

function viewQuery(view: PortalView): string {
  return `view=${view}`;
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function fetchFlights(view: PortalView): Promise<FlightSummary[]> {
  return getJson(`/api/flights?${viewQuery(view)}`);
}

export function fetchFlight(flightId: string, view: PortalView): Promise<FlightDetail> {
  return getJson(`/api/flight?${viewQuery(view)}&flight_id=${encodeURIComponent(flightId)}`);
}

export function fetchTelemetry(
  flightId: string,
  view: PortalView,
  since = 0,
): Promise<TelemetryPoint[]> {
  let url = `/api/telemetry?${viewQuery(view)}&flight_id=${encodeURIComponent(flightId)}`;
  if (since > 0) url += `&since=${since}`;
  return getJson(url);
}

export function fetchAlerts(
  view: PortalView,
  opts: { since?: number; flightId?: string; level?: string; limit?: number; skip?: number } = {},
): Promise<Alert[]> {
  const params = new URLSearchParams({ view });
  if (opts.since) params.set('since', String(opts.since));
  if (opts.flightId) params.set('flight_id', opts.flightId);
  if (opts.level) params.set('level', opts.level);
  if (opts.limit) params.set('limit', String(opts.limit));
  if (opts.skip) params.set('skip', String(opts.skip));
  return getJson(`/api/alerts?${params}`);
}

export function fetchZones(): Promise<ZonesData> {
  return getJson('/api/zones');
}
