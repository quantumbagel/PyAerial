import type { TelemetryPoint } from '../api/types';
import {
  formatAltitudeCell,
  formatDateTime,
  formatHeading,
  formatSpeedCell,
} from '../utils/format';

interface TelemetryTableProps {
  telemetry: TelemetryPoint[];
  selectedTelemetryPoint: TelemetryPoint | null;
  onSelectTelemetryPoint: (point: TelemetryPoint) => void;
  containerRef?: React.Ref<HTMLDivElement>;
}

export function TelemetryTable({
  telemetry,
  selectedTelemetryPoint,
  onSelectTelemetryPoint,
  containerRef,
}: TelemetryTableProps) {
  const handleKeyDown = (event: React.KeyboardEvent, point: TelemetryPoint) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onSelectTelemetryPoint(point);
    }
  };

  return (
    <div className="table-container" ref={containerRef}>
      <table className="tel-table">
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
        <tbody id="telemetry-table-body">
          {telemetry.length === 0 ? (
            <tr>
              <td colSpan={6} className="empty-cell">
                No telemetry data.
              </td>
            </tr>
          ) : (
            telemetry.map((point) => {
              const timeStr = formatDateTime(point.timestamp, { withSeconds: true });
              const latVal = point.latitude != null ? point.latitude.toFixed(4) : 'N/A';
              const lonVal = point.longitude != null ? point.longitude.toFixed(4) : 'N/A';
              const isSelected = selectedTelemetryPoint?.timestamp === point.timestamp;
              return (
                <tr
                  key={point.timestamp}
                  tabIndex={0}
                  role="button"
                  onClick={() => onSelectTelemetryPoint(point)}
                  onKeyDown={(event) => handleKeyDown(event, point)}
                  className={isSelected ? 'active-tel-row tel-row-interactive' : 'tel-row-interactive'}
                  aria-pressed={isSelected}
                >
                  <td>{timeStr}</td>
                  <td className="tel-num">{formatAltitudeCell(point.altitude)}</td>
                  <td className="tel-num">{formatSpeedCell(point.speed)}</td>
                  <td className="tel-num">{formatHeading(point.heading)}</td>
                  <td className="tel-num">{latVal}</td>
                  <td className="tel-num">{lonVal}</td>
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
