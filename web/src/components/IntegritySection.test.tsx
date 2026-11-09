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
    expect(screen.getByText('Score 0.12')).toBeInTheDocument();
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

describe('IntegritySection without evidence', () => {
  it('does not present the placeholder score of an unknown verdict', () => {
    render(<IntegritySection integrity={{ score: 1, status: 'unknown', checks: {} }} />);
    expect(screen.getByText('Not enough evidence')).toBeInTheDocument();
    expect(screen.queryByText(/Score/)).toBeNull();
  });

  it('rounds the transmitter offset and survives a missing score', () => {
    render(
      <IntegritySection
        integrity={{
          score: undefined as unknown as number,
          status: 'suspect',
          checks: { mystery: { status: 'skipped' as 'pass', detail: 'n/a' } },
          emitter: { latitude: 35.68, longitude: -78.72, offset_km: 12.3456 },
        }}
      />,
    );
    expect(screen.getByText('Suspect')).toBeInTheDocument();
    expect(screen.queryByText(/NaN|undefined/)).toBeNull();
    expect(screen.getByText(/^12\.3 km from the claimed position$/)).toBeInTheDocument();
  });

  it('explains when only ground rebroadcasts were heard', () => {
    render(
      <IntegritySection integrity={{ score: 1, status: 'unknown', checks: {}, rebroadcast: true }} />,
    );
    expect(screen.getByText(/TIS-B\/ADS-R/)).toBeInTheDocument();
  });
});
