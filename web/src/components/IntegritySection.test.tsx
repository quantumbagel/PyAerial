import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { IntegritySection } from './IntegritySection';

describe('IntegritySection', () => {
  it('lists failing checks, the transmitter and passing checks', () => {
    render(
      <IntegritySection
        integrity={{
          score: 0.12,
          status: 'spoofed',
          checks: {
            tdoa: { status: 'fail', detail: 'misses claimed position by 41.2 km' },
            timing: { status: 'pass', detail: 'ok' },
          },
          emitter: { latitude: 35.68, longitude: -78.72, offset_km: 88.4 },
        }}
      />,
    );
    expect(screen.getByText('Likely spoofed')).toBeInTheDocument();
    expect(screen.getByText('Score 0.12 of 1')).toBeInTheDocument();
    expect(screen.getByText('Arrival-time difference')).toBeInTheDocument();
    expect(screen.getByText('misses claimed position by 41.2 km')).toBeInTheDocument();
    expect(screen.getByText('88.4 km from the claimed position')).toBeInTheDocument();
    expect(screen.getByText('Transmit timing')).toBeInTheDocument();
  });

  it('renders nothing without an integrity document', () => {
    const { container } = render(<IntegritySection integrity={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
