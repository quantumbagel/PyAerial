import { sendWsRequest } from './liveSocket';
import type { Alert, AppConfig, FlightDetail, FlightSummary, PortalView, TelemetryPoint, ZonesData } from './types';

export function fetchFlights(view: PortalView): Promise<FlightSummary[]> {
  return sendWsRequest<FlightSummary[]>('fetchFlights', { view });
}

export function fetchFlight(flightId: string, view: PortalView): Promise<FlightDetail> {
  return sendWsRequest<FlightDetail>('fetchFlight', { flightId, view });
}

export function fetchTelemetry(
  flightId: string,
  view: PortalView,
  since = 0,
): Promise<TelemetryPoint[]> {
  return sendWsRequest<TelemetryPoint[]>('fetchTelemetry', { flightId, view, since });
}

export function fetchAlerts(
  view: PortalView,
  opts: { since?: number; flightId?: string; level?: string; limit?: number; skip?: number } = {},
): Promise<Alert[]> {
  return sendWsRequest<Alert[]>('fetchAlerts', { view, ...opts });
}

export function fetchZones(): Promise<ZonesData> {
  return sendWsRequest<ZonesData>('fetchZones');
}

export function fetchConfig(): Promise<AppConfig> {
  return sendWsRequest<AppConfig>('fetchConfig');
}
