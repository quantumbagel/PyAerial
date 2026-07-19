export interface FlightSummary {
  flight_id: string;
  icao: string;
  zone?: string;
  level?: string;
  start_time?: number;
  end_time?: number;
  callsign?: string | null;
  model?: string | null;
  owner?: string | null;
  country?: string | null;
  aircraft_type?: string | null;
  typecode?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  altitude?: number | null;
  speed?: number | null;
  heading?: number | null;
  is_live?: boolean;
  status?: string;
  retained?: boolean;
  timestamp?: number | null;
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
  zone?: string;
  level?: string;
}

export interface Alert {
  alert_id: string;
  flight_id?: string;
  icao?: string;
  callsign?: string;
  zone?: string;
  level?: string;
  timestamp?: number;
  eta?: number | null;
  altitude?: number | null;
  latitude?: number | null;
  longitude?: number | null;
}

export interface ZoneRule {
  name: string;
  when: Record<string, { min?: number; max?: number }>;
  dwell_seconds: number;
}

export interface Zone {
  name: string;
  coordinates: [number, number][];
  rules: ZoneRule[];
}

export interface ZonesData {
  home: { latitude: number; longitude: number };
  zones: Zone[];
}

export type PortalView = 'live' | 'history';

export type LiveMessage =
  | { type: 'flights'; flights: FlightSummary[] }
  | { type: 'telemetry'; telemetry: TelemetryPoint[]; timestamp: number }
  | { type: 'alerts'; alerts: Alert[] };

export interface AppConfig {
  home: { latitude: number; longitude: number };
  remember_planes: number;
  hz: number;
  duplicate_packet_merging: number;
}
