import { useId, type ButtonHTMLAttributes, type HTMLAttributes, type KeyboardEvent, type ReactNode } from 'react';
import { cn } from './cn';

/**
 * Unique, paired ids for a tab set: `tab(name)` for the tab button and
 * `panel(name)` for its panel. Two tab sets never collide.
 */
export function useTabIds() {
  const base = useId();
  return {
    tab: (name: string) => `${base}-tab-${name}`,
    panel: (name: string) => `${base}-panel-${name}`,
  };
}

interface TabListProps extends HTMLAttributes<HTMLDivElement> {
  compact?: boolean;
  children: ReactNode;
}

interface TabProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  id: string;
  controls: string;
  active?: boolean;
  children: ReactNode;
}

interface TabPanelProps extends HTMLAttributes<HTMLDivElement> {
  id: string;
  labelledBy: string;
  active: boolean;
  children?: ReactNode;
}

/** Arrow/Home/End move between tabs and activate them (automatic activation). */
export function TabList({ compact = false, className, children, onKeyDown, ...props }: TabListProps) {
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    onKeyDown?.(event);
    if (event.defaultPrevented) return;
    if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
    const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]'));
    const current = tabs.indexOf(document.activeElement as HTMLElement);
    if (current < 0) return;
    event.preventDefault();
    let next = current;
    if (event.key === 'ArrowRight') next = (current + 1) % tabs.length;
    if (event.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    tabs[next]?.focus();
    tabs[next]?.click();
  };

  return (
    <div
      role="tablist"
      className={cn('ui-tablist', compact && 'ui-tablist--compact', className)}
      onKeyDown={handleKeyDown}
      {...props}
    >
      {children}
    </div>
  );
}

export function Tab({ id, controls, active = false, className, children, ...props }: TabProps) {
  return (
    <button
      type="button"
      role="tab"
      id={id}
      aria-controls={controls}
      className={cn('ui-tab', active && 'is-active', className)}
      aria-selected={active}
      tabIndex={active ? 0 : -1}
      {...props}
    >
      {children}
    </button>
  );
}

/** Inactive panels use the `hidden` attribute (everywhere, one mechanism). */
export function TabPanel({ id, labelledBy, active, className, children, ...props }: TabPanelProps) {
  return (
    <div
      role="tabpanel"
      id={id}
      aria-labelledby={labelledBy}
      hidden={!active}
      className={className}
      {...props}
    >
      {children}
    </div>
  );
}
