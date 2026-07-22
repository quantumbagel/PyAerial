import { useEffect, useRef, useState } from 'react';
import { ALERT_SORT_OPTIONS, type AlertSortField, type SortDirection } from '../utils/alertData';

interface AlertSortControlsProps {
  alertSortField: AlertSortField;
  alertSortDirection: SortDirection;
  onAlertSortChange: (field: AlertSortField) => void;
  onAlertSortDirectionToggle: () => void;
}

export function AlertSortControls({
  alertSortField,
  alertSortDirection,
  onAlertSortChange,
  onAlertSortDirectionToggle,
}: AlertSortControlsProps) {
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

  const selectedOption = ALERT_SORT_OPTIONS.find((o) => o.value === alertSortField);

  return (
    <div id="alert-sort-bar" className="flight-sort-bar" ref={containerRef}>
      <label htmlFor="alert-sort-field" className="flight-sort-label">
        Sort by
      </label>
      <div className="flight-sort-dropdown-container">
        <button
          type="button"
          className="flight-sort-trigger-btn"
          aria-haspopup="listbox"
          aria-expanded={isOpen}
          onClick={() => setIsOpen((prev) => !prev)}
        >
          <span key={alertSortField}>{selectedOption?.label ?? 'Activated'}</span>
          <span className="sort-chevron">{isOpen ? '▲' : '▼'}</span>
        </button>

        <select
          id="alert-sort-field"
          className="flight-sort-select-hidden"
          value={alertSortField}
          onChange={(e) => onAlertSortChange(e.target.value as AlertSortField)}
          tabIndex={-1}
          aria-hidden="true"
        >
          {ALERT_SORT_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>

        {isOpen && (
          <ul className="flight-sort-menu" role="listbox" aria-label="Alert sort options">
            {ALERT_SORT_OPTIONS.map((option) => (
              <li
                key={option.value}
                role="option"
                aria-selected={option.value === alertSortField}
                className={`flight-sort-menu-item${option.value === alertSortField ? ' selected' : ''}`}
                onClick={() => {
                  onAlertSortChange(option.value);
                  setIsOpen(false);
                }}
              >
                {option.label}
              </li>
            ))}
          </ul>
        )}
      </div>
      <button
        type="button"
        className="flight-sort-direction"
        onClick={onAlertSortDirectionToggle}
        title={alertSortDirection === 'asc' ? 'Ascending' : 'Descending'}
        aria-label={alertSortDirection === 'asc' ? 'Sort ascending' : 'Sort descending'}
      >
        {alertSortDirection === 'asc' ? '↑' : '↓'}
      </button>
    </div>
  );
}
