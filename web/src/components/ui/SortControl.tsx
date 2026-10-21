import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { Button } from './Button';

export type SortDirection = 'asc' | 'desc';

interface SortControlProps<F extends string> {
  /** What is being sorted, for screen readers ("flights", "alerts"). */
  noun: string;
  options: { value: F; label: string }[];
  value: F;
  direction: SortDirection;
  onChange: (value: F) => void;
  onToggleDirection: () => void;
}

/**
 * Sort field picker (button + listbox popup) and a direction toggle.
 * Opening moves focus into the list; arrows/Home/End move a roving tabindex;
 * Enter/Space pick; Escape closes without letting the drawer see it.
 */
export function SortControl<F extends string>({
  noun,
  options,
  value,
  direction,
  onChange,
  onToggleDirection,
}: SortControlProps<F>) {
  const [open, setOpen] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<(HTMLLIElement | null)[]>([]);
  const baseId = useId();
  const labelId = `${baseId}-label`;
  const buttonId = `${baseId}-button`;
  const listId = `${baseId}-list`;
  const selectedIndex = Math.max(
    0,
    options.findIndex((o) => o.value === value),
  );
  const selected = options[selectedIndex];

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open]);

  useEffect(() => {
    if (open) optionRefs.current[focusIndex]?.focus();
  }, [open, focusIndex]);

  const openMenu = (index = selectedIndex) => {
    setFocusIndex(index);
    setOpen(true);
  };

  const close = (refocus: boolean) => {
    setOpen(false);
    if (refocus) buttonRef.current?.focus();
  };

  const choose = (index: number) => {
    const option = options[index];
    if (option && option.value !== value) onChange(option.value);
    close(true);
  };

  const onButtonKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      openMenu(e.key === 'ArrowUp' ? options.length - 1 : selectedIndex);
    }
  };

  const onListKeyDown = (e: KeyboardEvent<HTMLUListElement>) => {
    const last = options.length - 1;
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        setFocusIndex((i) => (i >= last ? 0 : i + 1));
        break;
      case 'ArrowUp':
        e.preventDefault();
        setFocusIndex((i) => (i <= 0 ? last : i - 1));
        break;
      case 'Home':
        e.preventDefault();
        setFocusIndex(0);
        break;
      case 'End':
        e.preventDefault();
        setFocusIndex(last);
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        choose(focusIndex);
        break;
      case 'Escape':
        // Handled here: don't let the global Escape (drawer close) see it.
        e.preventDefault();
        e.stopPropagation();
        close(true);
        break;
      case 'Tab':
        close(false);
        break;
      default:
        break;
    }
  };

  return (
    <div className="ui-sort-bar" ref={containerRef}>
      <span id={labelId} className="ui-sort-label">
        Sort {noun} by
      </span>
      <div className="ui-sort-dropdown">
        <Button
          ref={buttonRef}
          id={buttonId}
          variant="subtle"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={open ? listId : undefined}
          aria-labelledby={`${labelId} ${buttonId}`}
          onClick={() => (open ? close(false) : openMenu())}
          onKeyDown={onButtonKeyDown}
        >
          <span>{selected?.label}</span>
          <span className="ui-chevron" aria-hidden="true">
            {open ? '▲' : '▼'}
          </span>
        </Button>
        {open && (
          <ul
            id={listId}
            className="ui-menu"
            role="listbox"
            aria-labelledby={labelId}
            onKeyDown={onListKeyDown}
          >
            {options.map((option, index) => (
              <li
                key={option.value}
                ref={(el) => {
                  optionRefs.current[index] = el;
                }}
                role="option"
                tabIndex={index === focusIndex ? 0 : -1}
                aria-selected={option.value === value}
                className={`ui-menu-item${option.value === value ? ' is-selected' : ''}`}
                onClick={() => choose(index)}
              >
                {option.label}
              </li>
            ))}
          </ul>
        )}
      </div>
      <Button
        variant="icon"
        onClick={onToggleDirection}
        title={direction === 'asc' ? 'Ascending (click for descending)' : 'Descending (click for ascending)'}
        aria-label={`Sort direction: ${direction === 'asc' ? 'ascending' : 'descending'}`}
      >
        <span aria-hidden="true">{direction === 'asc' ? '↑' : '↓'}</span>
      </Button>
    </div>
  );
}
