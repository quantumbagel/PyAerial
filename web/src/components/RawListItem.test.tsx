import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { BufferedRawFrame } from '../api/types';
import { RawListItem } from './RawListItem';

function row(overrides: Partial<BufferedRawFrame> = {}): BufferedRawFrame {
  return {
    id: '1',
    hex: '8d406b902015a678d4d220aa4bda',
    timestamp: 1,
    df: 17,
    icao: '406b90',
    ...overrides,
  };
}

describe('RawListItem', () => {
  it('shows every receiver that heard the frame', () => {
    render(
      <RawListItem
        frame={row({ receiver: 'strong', receivers: ['strong', 'weak'] })}
        selectable={false}
      />,
    );
    expect(screen.getByTitle(/strong · weak/)).toBeInTheDocument();
    expect(screen.getByText(/strong · weak/)).toBeInTheDocument();
  });

  it('falls back to a single receiver name', () => {
    render(<RawListItem frame={row({ receiver: 'main' })} selectable={false} />);
    expect(screen.getByText(/main/)).toBeInTheDocument();
  });
});
