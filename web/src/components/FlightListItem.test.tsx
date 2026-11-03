import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { makeFlight } from '../test/fixtures';
import { FlightListItem } from './FlightListItem';

function renderItem(interrupted?: boolean) {
  render(
    <ul>
      <FlightListItem
        flight={makeFlight({ status: 'completed', is_live: false, interrupted })}
        active={false}
        sortField="last_seen"
        onSelect={() => {}}
      />
    </ul>,
  );
}

describe('FlightListItem', () => {
  it('marks a flight the engine did not finalize as interrupted', () => {
    renderItem(true);
    expect(screen.getByText('Interrupted')).toBeInTheDocument();
  });

  it('shows no badge for a finalized flight', () => {
    renderItem(undefined);
    expect(screen.queryByText('Interrupted')).not.toBeInTheDocument();
  });
});
