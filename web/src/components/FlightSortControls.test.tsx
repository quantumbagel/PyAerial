import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { FlightSortControls } from './FlightSortControls';

describe('FlightSortControls', () => {
  const defaultProps = {
    flightSortField: 'last_seen' as const,
    flightSortDirection: 'desc' as const,
    onFlightSortChange: vi.fn(),
    onFlightSortDirectionToggle: vi.fn(),
  };

  it('renders correctly with current sort selection', () => {
    render(<FlightSortControls {...defaultProps} />);
    expect(screen.getByText('Sort by')).not.toBeNull();
    expect(screen.getByRole('button', { name: /Last Seen/i })).not.toBeNull();
  });

  it('opens sort menu on trigger button click and allows selecting options', () => {
    const onFlightSortChange = vi.fn();
    render(<FlightSortControls {...defaultProps} onFlightSortChange={onFlightSortChange} />);

    const triggerBtn = screen.getByRole('button', { name: /Last Seen/i });
    expect(screen.queryByRole('listbox')).toBeNull();

    fireEvent.click(triggerBtn);
    expect(screen.getByRole('listbox')).not.toBeNull();

    const callsignOption = screen.getByRole('option', { name: 'Callsign' });
    fireEvent.click(callsignOption);

    expect(onFlightSortChange).toHaveBeenCalledWith('callsign');
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('toggles sort direction when direction button is clicked', () => {
    const onFlightSortDirectionToggle = vi.fn();
    render(
      <FlightSortControls
        {...defaultProps}
        onFlightSortDirectionToggle={onFlightSortDirectionToggle}
      />,
    );

    const dirButton = screen.getByRole('button', { name: /Sort descending/i });
    fireEvent.click(dirButton);

    expect(onFlightSortDirectionToggle).toHaveBeenCalledTimes(1);
  });

  it('keeps sort menu open when parent component re-renders', () => {
    const { rerender } = render(<FlightSortControls {...defaultProps} />);

    const triggerBtn = screen.getByRole('button', { name: /Last Seen/i });
    fireEvent.click(triggerBtn);
    expect(screen.getByRole('listbox')).not.toBeNull();

    // Re-render with identical props (simulating parent re-render during plane update)
    rerender(<FlightSortControls {...defaultProps} />);
    expect(screen.getByRole('listbox')).not.toBeNull();
  });
});
