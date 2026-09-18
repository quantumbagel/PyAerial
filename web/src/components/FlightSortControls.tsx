import { memo, useEffect, useRef, useState } from 'react';
import { FLIGHT_SORT_OPTIONS, type FlightSortField, type SortDirection } from '../utils/flightData';
import { Button } from './ui';

interface FlightSortControlsProps {
  flightSortField: FlightSortField;
  flightSortDirection: SortDirection;
  onFlightSortChange: (field: FlightSortField) => void;
  onFlightSortDirectionToggle: () => void;
}

export const FlightSortControls = memo(function FlightSortControls({
  flightSortField,
  flightSortDirection,
  onFlightSortChange,
  onFlightSortDirectionToggle,
}: FlightSortControlsProps) {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;

    const handleClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setIsOpen(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen]);

  const selectedOption = FLIGHT_SORT_OPTIONS.find((o) => o.value === flightSortField);

  return (
    <div id="flight-sort-bar" className="ui-sort-bar" ref={containerRef}>
      <label htmlFor="flight-sort-field" className="ui-sort-label">
        Sort by
      </label>
      <div className="ui-sort-dropdown">
        <Button
          variant="subtle"
          aria-haspopup="listbox"
          aria-expanded={isOpen}
          aria-controls="flight-sort-menu"
          onClick={() => setIsOpen((prev) => !prev)}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              setIsOpen(true);
            }
          }}
        >
          <span>{selectedOption?.label ?? 'Last Seen'}</span>
          <span className="ui-chevron">{isOpen ? '▲' : '▼'}</span>
        </Button>

        <select
          id="flight-sort-field"
          className="ui-select-hidden"
          value={flightSortField}
          onChange={(e) => onFlightSortChange(e.target.value as FlightSortField)}
          tabIndex={-1}
        >
          {FLIGHT_SORT_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>

        {isOpen && (
          <ul id="flight-sort-menu" className="ui-menu" role="listbox" aria-label="Sort options">
            {FLIGHT_SORT_OPTIONS.map((option) => (
              <li
                key={option.value}
                role="option"
                tabIndex={0}
                aria-selected={option.value === flightSortField}
                className={`ui-menu-item${option.value === flightSortField ? ' is-selected' : ''}`}
                onClick={() => {
                  onFlightSortChange(option.value);
                  setIsOpen(false);
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    onFlightSortChange(option.value);
                    setIsOpen(false);
                  } else if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    (e.currentTarget.nextElementSibling as HTMLElement | null)?.focus();
                  } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    (e.currentTarget.previousElementSibling as HTMLElement | null)?.focus();
                  }
                }}
              >
                {option.label}
              </li>
            ))}
          </ul>
        )}
      </div>
      <Button
        variant="icon"
        onClick={onFlightSortDirectionToggle}
        title={flightSortDirection === 'asc' ? 'Ascending' : 'Descending'}
        aria-label={flightSortDirection === 'asc' ? 'Sort ascending' : 'Sort descending'}
      >
        {flightSortDirection === 'asc' ? '↑' : '↓'}
      </Button>
    </div>
  );
});
