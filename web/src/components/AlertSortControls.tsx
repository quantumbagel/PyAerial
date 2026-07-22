import { useEffect, useRef, useState } from 'react';
import { ALERT_SORT_OPTIONS, type AlertSortField, type SortDirection } from '../utils/alertData';
import { Button } from './ui';

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
    <div id="alert-sort-bar" className="ui-sort-bar" ref={containerRef}>
      <label htmlFor="alert-sort-field" className="ui-sort-label">
        Sort by
      </label>
      <div className="ui-sort-dropdown">
        <Button
          variant="subtle"
          aria-haspopup="listbox"
          aria-expanded={isOpen}
          onClick={() => setIsOpen((prev) => !prev)}
        >
          <span key={alertSortField}>{selectedOption?.label ?? 'Activated'}</span>
          <span className="ui-chevron">{isOpen ? '▲' : '▼'}</span>
        </Button>

        <select
          id="alert-sort-field"
          className="ui-select-hidden"
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
          <ul className="ui-menu" role="listbox" aria-label="Alert sort options">
            {ALERT_SORT_OPTIONS.map((option) => (
              <li
                key={option.value}
                role="option"
                aria-selected={option.value === alertSortField}
                className={`ui-menu-item${option.value === alertSortField ? ' is-selected' : ''}`}
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
      <Button
        variant="icon"
        onClick={onAlertSortDirectionToggle}
        title={alertSortDirection === 'asc' ? 'Ascending' : 'Descending'}
        aria-label={alertSortDirection === 'asc' ? 'Sort ascending' : 'Sort descending'}
      >
        {alertSortDirection === 'asc' ? '↑' : '↓'}
      </Button>
    </div>
  );
}
