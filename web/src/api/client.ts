import { sendWsRequest } from './liveSocket';
import type { Alert, AppConfig, FlightDetail, FlightSummary, PortalView, ServerStats, TelemetryPoint, ZonesData } from './types';

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
  opts: {
    since?: number;
    flightId?: string;
    rule?: string;
    limit?: number;
    skip?: number;
    activeOnly?: boolean;
  } = {},
): Promise<Alert[]> {
  const { activeOnly, flightId, ...rest } = opts;
  return sendWsRequest<Alert[]>('fetchAlerts', {
    view,
    flightId,
    active_only: activeOnly,
    ...rest,
  });
}

export function fetchStats(view?: PortalView): Promise<ServerStats> {
  return sendWsRequest<ServerStats>('fetchStats', { view });
}

export function fetchZones(): Promise<ZonesData> {
  return sendWsRequest<ZonesData>('fetchZones');
}

export function fetchConfig(): Promise<AppConfig> {
  return sendWsRequest<AppConfig>('fetchConfig');
}
