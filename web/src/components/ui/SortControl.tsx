import { useId } from 'react';
import type { SortDirection } from '../../utils/sort';
import { Button } from './Button';

interface SortControlProps<F extends string> {
  /** What is being sorted, for screen readers ("flights", "alerts"). */
  noun: string;
  fields: readonly { id: F; label: string }[];
  value: F;
  direction: SortDirection;
  onChange: (value: F) => void;
  onToggleDirection: () => void;
}

/** Native field picker plus a direction toggle. */
export function SortControl<F extends string>({
  noun,
  fields,
  value,
  direction,
  onChange,
  onToggleDirection,
}: SortControlProps<F>) {
  const selectId = useId();
  const ascending = direction === 'asc';

  return (
    <div className="ui-sort-bar">
      <label htmlFor={selectId} className="ui-sort-label">
        Sort {noun} by
      </label>
      <select
        id={selectId}
        className="ui-input ui-sort-select"
        value={value}
        onChange={(e) => {
          const next = fields.find((field) => field.id === e.target.value);
          if (next) onChange(next.id);
        }}
      >
        {fields.map((field) => (
          <option key={field.id} value={field.id}>
            {field.label}
          </option>
        ))}
      </select>
      <Button
        variant="icon"
        onClick={onToggleDirection}
        title={ascending ? 'Ascending (click for descending)' : 'Descending (click for ascending)'}
        aria-label={`Sort direction: ${ascending ? 'ascending' : 'descending'}`}
      >
        <span aria-hidden="true">{ascending ? '↑' : '↓'}</span>
      </Button>
    </div>
  );
}
