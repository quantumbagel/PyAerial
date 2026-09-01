import { sendWsRequest } from './liveSocket';
import type { Alert, AppConfig, FlightDetail, FlightSummary, PortalView, ServerStats, TelemetryPoint, ZonesData } from './types';

export type HistoryListOpts = {
  skip?: number;
  limit?: number;
  q?: string;
  since?: number;
  until?: number;
};

function historyParams(
  view: PortalView,
  opts: HistoryListOpts = {},
): Record<string, unknown> {
  const params: Record<string, unknown> = { view };
  if (opts.skip) params.skip = opts.skip;
  if (opts.limit) params.limit = opts.limit;
  if (opts.q) params.q = opts.q;
  if (opts.since != null) params.since = opts.since;
  if (opts.until != null) params.until = opts.until;
  return params;
}

export function fetchFlights(
  view: PortalView,
  opts: HistoryListOpts = {},
): Promise<FlightSummary[]> {
  return sendWsRequest<FlightSummary[]>('fetchFlights', historyParams(view, opts));
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
    until?: number;
    flightId?: string;
    rule?: string;
    q?: string;
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

export function fetchStats(): Promise<ServerStats> {
  return sendWsRequest<ServerStats>('fetchStats');
}

export function fetchZones(): Promise<ZonesData> {
  return sendWsRequest<ZonesData>('fetchZones');
}

export function fetchConfig(): Promise<AppConfig> {
  return sendWsRequest<AppConfig>('fetchConfig');
}
