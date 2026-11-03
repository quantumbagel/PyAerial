import { render, screen } from '@testing-library/react';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import type { TelemetryPoint } from '../api/types';
import { setFormatLocale } from '../utils/format';
import { TelemetryTable, telemetryRowKeys } from './TelemetryTable';
import { makePoint } from '../test/fixtures';

beforeAll(() => setFormatLocale('en-US'));
afterAll(() => setFormatLocale(undefined));

const point = (timestamp: number, extra: Partial<TelemetryPoint> = {}): TelemetryPoint => ({
  ...makePoint(),
  timestamp,
  latitude: 35,
  longitude: -78,
  altitude: 1000,
  ...extra,
});

function renderTable(telemetry: TelemetryPoint[], truncated = false) {
  return render(
    <TelemetryTable
      telemetry={telemetry}
      selectedTelemetryPoint={null}
      onSelectTelemetryPoint={() => {}}
      truncated={truncated}
    />,
  );
}

describe('TelemetryTable', () => {
  it('keys rows uniquely and stably when timestamps repeat', () => {
    const rows = [point(1), point(2), point(2), point(3)];
    expect(telemetryRowKeys(rows)).toEqual(['1:0', '2:0', '2:1', '3:0']);
    // Dropping the oldest row (the table keeps the newest) does not shift the others.
    expect(telemetryRowKeys(rows.slice(1))).toEqual(['2:0', '2:1', '3:0']);

    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderTable(rows);
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
    expect(screen.getAllByRole('row')).toHaveLength(5);
  });

  it('adds a labelled GNSS height column only when a point carries one', () => {
    const { unmount } = renderTable([point(1), point(2)]);
    expect(screen.queryByText('GNSS height')).toBeNull();
    expect(screen.getByText('Altitude')).toBeInTheDocument();
    unmount();

    renderTable([point(1), point(2, { gnss_height: 1050.4 })]);
    const header = screen.getByText('GNSS height');
    expect(header).toHaveAttribute('title', expect.stringMatching(/WGS84 ellipsoid/));
    expect(screen.getByText('Baro altitude')).toBeInTheDocument();
    expect(screen.getByText('1,050 m')).toBeInTheDocument();
  });

  it('notes when older telemetry was left out', () => {
    const { unmount } = renderTable([point(1)]);
    expect(screen.queryByText(/left out/)).toBeNull();
    unmount();
    renderTable([point(1)], true);
    expect(screen.getByText(/left out/)).toBeInTheDocument();
  });
});
