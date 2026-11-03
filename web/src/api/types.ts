// Wire types are generated from the server's pydantic models
// (src/pyaerial/api/models.py) by `python scripts/gen_types.py`; see
// generated.ts. Only portal-side types and the /ws/raw frames live here.
import type { AppConfig, Integrity } from './generated';

export type {
  ActiveAlert,
  Alert,
  AlertStats,
  AppConfig,
  ErrorResponse,
  FetchTelemetryResponse,
  FlightDetail,
  FlightSummary,
  HelloMessage,
  Integrity,
  IntegrityCheck,
  LiveMessage,
  ReceiverInfo,
  ServerStats,
  TelemetryPoint,
  Zone,
  ZoneRule,
  ZonesData,
} from './generated';

export type IntegrityStatus = Integrity['status'];

export type PortalView = 'live' | 'history';

/** `busy`: the server closed us at its connection cap (1013); we keep retrying. */
export type WsStatus = 'connecting' | 'connected' | 'reconnecting' | 'busy' | 'rejected';

export interface RawFrame {
  hex: string;
  timestamp: number;
  receiver?: string;
  /** Every receiver that heard this frame, strongest copy first. */
  receivers?: string[];
  rssi?: number;
  clock?: number;
  df?: number;
  icao?: string;
}

export interface BufferedRawFrame extends RawFrame {
  id: string;
}

/** Station and receivers shown on the Raw tab (from `fetchConfig`). */
export type RawAntenna = Pick<AppConfig, 'home' | 'receivers'>;
