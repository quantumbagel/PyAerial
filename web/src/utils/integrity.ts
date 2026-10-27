import type { Integrity, IntegrityCheck } from '../api/types';

/** Reader-facing names for the engine's integrity check keys. */
const CHECK_LABELS: Record<string, string> = {
  aoa: 'Angle of arrival',
  tdoa: 'Arrival-time difference',
  mode_s_altitude: 'Mode S altitude',
  radio_horizon: 'Radio horizon',
  duplicate_icao: 'Duplicate address',
  cross_network: 'Public network',
  rssi_range: 'Signal vs. range',
  rssi_trend: 'Signal trend',
  kinematics: 'Kinematics',
  timing: 'Transmit timing',
  replay: 'Replayed frames',
  emitter_cluster: 'Shared transmitter',
  mode_s_corroboration: 'Mode S replies',
  message_mix: 'Message mix',
  appearance: 'Appearance',
  identity: 'Identity',
};

const STATUS_RANK: Record<IntegrityCheck['status'], number> = { fail: 0, warn: 1, pass: 2 };

export function checkLabel(name: string): string {
  return CHECK_LABELS[name] ?? name.replace(/_/g, ' ');
}

/** True for aircraft the list and map should call out. */
export function isFlagged(integrity: Integrity | null | undefined): boolean {
  return integrity?.status === 'suspect' || integrity?.status === 'spoofed';
}

export function integrityLabel(integrity: Integrity | null | undefined): string {
  switch (integrity?.status) {
    case 'spoofed':
      return 'Likely spoofed';
    case 'suspect':
      return 'Suspect';
    case 'ok':
      return 'Consistent';
    default:
      return 'Not enough evidence';
  }
}

/** Failing checks first, then warnings, then passes; alphabetical within each. */
export function sortedChecks(integrity: Integrity): Array<[string, IntegrityCheck]> {
  return Object.entries(integrity.checks ?? {}).sort(
    ([a, ca], [b, cb]) =>
      STATUS_RANK[ca.status] - STATUS_RANK[cb.status] || checkLabel(a).localeCompare(checkLabel(b)),
  );
}
