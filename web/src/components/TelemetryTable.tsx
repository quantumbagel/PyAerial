import { memo } from 'react';
import type { TelemetryPoint } from '../api/types';
import {
  formatAltitudeCompact,
  formatCoordinate,
  formatDateTime,
  formatHeading,
  formatSpeedCompact,
} from '../utils/format';
import { StateView } from './ui';

/** Rows rendered (newest kept); the selected flight buffers up to 5k points. */
export const TELEMETRY_TABLE_ROWS = 250;

interface TelemetryTableProps {
  telemetry: TelemetryPoint[];
  selectedTelemetryPoint: TelemetryPoint | null;
  onSelectTelemetryPoint: (point: TelemetryPoint) => void;
  containerRef?: React.Ref<HTMLDivElement>;
}

export const TelemetryTable = memo(function TelemetryTable({
  telemetry,
  selectedTelemetryPoint,
  onSelectTelemetryPoint,
  containerRef,
}: TelemetryTableProps) {
  if (telemetry.length === 0) {
    return <StateView layout="inline">No telemetry yet.</StateView>;
  }
  const rows = telemetry.length > TELEMETRY_TABLE_ROWS ? telemetry.slice(-TELEMETRY_TABLE_ROWS) : telemetry;
  const selectedTs = selectedTelemetryPoint?.timestamp;
  return (
    <div className="table-container" ref={containerRef}>
      <table className="tel-table">
        <caption className="visually-hidden">
          Telemetry fixes, oldest first. Choose a time to show that fix on the map.
        </caption>
        <thead>
          <tr>
            <th scope="col">Time</th>
            <th scope="col" className="tel-num">Altitude</th>
            <th scope="col" className="tel-num">Speed</th>
            <th scope="col" className="tel-num">Heading</th>
            <th scope="col" className="tel-num">Latitude</th>
            <th scope="col" className="tel-num">Longitude</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((point) => {
            const isSelected = selectedTs != null && selectedTs === point.timestamp;
            return (
              <tr
                key={point.timestamp}
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
  );
});
