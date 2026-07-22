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
          onClick={() => setIsOpen((prev) => !prev)}
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
          aria-hidden="true"
        >
          {FLIGHT_SORT_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>

        {isOpen && (
          <ul className="ui-menu" role="listbox" aria-label="Sort options">
            {FLIGHT_SORT_OPTIONS.map((option) => (
              <li
                key={option.value}
                role="option"
                aria-selected={option.value === flightSortField}
                className={`ui-menu-item${option.value === flightSortField ? ' is-selected' : ''}`}
                onClick={() => {
                  onFlightSortChange(option.value);
                  setIsOpen(false);
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
