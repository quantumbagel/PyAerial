export interface PortalProjection {
  horizon_seconds: number;
  step_seconds?: number;
  track_path: [number, number][];
  intent_path?: [number, number][] | null;
  selected_heading?: number | null;
  selected_altitude?: number | null;
  motion_heading?: number;
  motion_speed_kph?: number;
  turn_rate_deg_s?: number;
}

export interface AlertStats {
  episode_count: number;
  total_seconds: number;
  active_count?: number;
}

export interface ActiveAlert {
  alert_id?: string;
  zone: string;
  rule: string;
  activated_at?: number;
  eta?: number | null;
}

export interface FlightSummary {
  flight_id: string;
  icao: string;
  active_alerts?: ActiveAlert[];
  alert_stats?: AlertStats;
  start_time?: number;
  end_time?: number;
  callsign?: string | null;
  model?: string | null;
  owner?: string | null;
  country?: string | null;
  aircraft_type?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  altitude?: number | null;
  speed?: number | null;
  heading?: number | null;
  is_live?: boolean;
  status?: string;
  retained?: boolean;
  timestamp?: number | null;
  portal_projection?: PortalProjection | null;
}

export interface FlightDetail extends FlightSummary {
  registration?: string | null;
  photo_url?: string | null;
  photo_photographer?: string | null;
  photo_link?: string | null;
}

export interface TelemetryPoint {
  timestamp: number;
  latitude?: number | null;
  longitude?: number | null;
  altitude?: number | null;
  speed?: number | null;
  heading?: number | null;
  flight_id?: string;
  icao?: string;
  active_alerts?: ActiveAlert[];
}

export interface Alert {
  alert_id: string;
  flight_id?: string;
  icao?: string;
  callsign?: string;
  zone?: string;
  rule?: string;
  active?: boolean;
  activated_at?: number;
  deactivated_at?: number | null;
  eta?: number | null;
  altitude?: number | null;
  latitude?: number | null;
  longitude?: number | null;
}

export interface ZoneRule {
  name: string;
  color?: string;
  when: Record<string, { min?: number; max?: number }>;
  dwell_seconds: number;
}

export interface Zone {
  name: string;
  color?: string;
  coordinates: [number, number][];
  rules: ZoneRule[];
}

export interface ZonesData {
  home: { latitude: number; longitude: number };
  zones: Zone[];
  alert_colors?: Record<string, string>;
}

export type PortalView = 'live' | 'history';

export interface ServerStats {
  live_flights: number;
  active_alerts: number;
  retained_flights: number;
  historical_alerts: number;
}

export type LiveMessage =
  | { type: 'flights'; flights: FlightSummary[] }
  | { type: 'telemetry'; telemetry: TelemetryPoint[]; timestamp: number }
  | { type: 'alerts'; alerts: Alert[] }
  | { type: 'stats'; stats: ServerStats };

export interface AppConfig {
  home: { latitude: number; longitude: number };
  remember_planes: number;
  projection_seconds?: number;
}
