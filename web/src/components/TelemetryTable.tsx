import { memo } from 'react';
import type { TelemetryPoint } from '../api/types';
import {
  formatAltitudeCompact,
  formatCoordinate,
  formatDateTime,
  formatHeading,
  formatSpeedCompact,
  isFiniteNumber,
} from '../utils/format';
import { StateView } from './ui';

/** Rows rendered (newest kept); the selected flight buffers up to 5k points. */
export const TELEMETRY_TABLE_ROWS = 250;

export const GNSS_HEIGHT_TITLE =
  'Geometric height above the WGS84 ellipsoid, from GNSS positions (not barometric altitude)';

interface TelemetryTableProps {
  telemetry: TelemetryPoint[];
  selectedTelemetryPoint: TelemetryPoint | null;
  onSelectTelemetryPoint: (point: TelemetryPoint) => void;
  containerRef?: React.Ref<HTMLDivElement>;
  /** The server left this flight's oldest points out. */
  truncated?: boolean;
}

/** Row keys that stay put as rows scroll in and out, even for repeated timestamps. */
export function telemetryRowKeys(rows: TelemetryPoint[]): string[] {
  const seen = new Map<number, number>();
  return rows.map((point) => {
    const n = seen.get(point.timestamp) ?? 0;
    seen.set(point.timestamp, n + 1);
    return `${point.timestamp}:${n}`;
  });
}

export const TelemetryTable = memo(function TelemetryTable({
  telemetry,
  selectedTelemetryPoint,
  onSelectTelemetryPoint,
  containerRef,
  truncated = false,
}: TelemetryTableProps) {
  if (telemetry.length === 0) {
    return <StateView layout="inline">No telemetry yet.</StateView>;
  }
  const rows = telemetry.length > TELEMETRY_TABLE_ROWS ? telemetry.slice(-TELEMETRY_TABLE_ROWS) : telemetry;
  const keys = telemetryRowKeys(rows);
  const selectedTs = selectedTelemetryPoint?.timestamp;
  // Only TC 20-22 (GNSS) positions carry it; most flights never do.
  const showGnss = rows.some((point) => isFiniteNumber(point.gnss_height));
  return (
    <>
      {truncated && (
        <p className="tel-note">Older telemetry for this flight was left out; the newest points are shown.</p>
      )}
      <div className="table-container" ref={containerRef}>
        <table className="tel-table">
          <caption className="visually-hidden">
            Telemetry fixes, oldest first. Choose a time to show that fix on the map.
          </caption>
          <thead>
            <tr>
              <th scope="col">Time</th>
              <th scope="col" className="tel-num">
                {showGnss ? 'Baro altitude' : 'Altitude'}
              </th>
              {showGnss && (
                <th scope="col" className="tel-num" title={GNSS_HEIGHT_TITLE}>
                  GNSS height
                </th>
              )}
              <th scope="col" className="tel-num">Speed</th>
              <th scope="col" className="tel-num">Heading</th>
              <th scope="col" className="tel-num">Latitude</th>
              <th scope="col" className="tel-num">Longitude</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((point, index) => {
              const isSelected = selectedTs != null && selectedTs === point.timestamp;
              return (
                <tr
                  key={keys[index]}
                  className={isSelected ? 'is-selected' : undefined}
                  // Mouse users can click anywhere on the row; keyboard users get the button.
                  onClick={() => onSelectTelemetryPoint(point)}
                >
                  <td>
                    <button
                      type="button"
                      className="tel-select"
                      aria-pressed={isSelected}
                      onClick={(event) => {
                        event.stopPropagation();
                        onSelectTelemetryPoint(point);
                      }}
                    >
                      {formatDateTime(point.timestamp, { withSeconds: true })}
                    </button>
                  </td>
                  <td className="tel-num">{formatAltitudeCompact(point.altitude)}</td>
                  {showGnss && <td className="tel-num">{formatAltitudeCompact(point.gnss_height)}</td>}
                  <td className="tel-num">{formatSpeedCompact(point.speed)}</td>
                  <td className="tel-num">{formatHeading(point.heading)}</td>
                  <td className="tel-num">{formatCoordinate(point.latitude)}</td>
                  <td className="tel-num">{formatCoordinate(point.longitude)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
});
