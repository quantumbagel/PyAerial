import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from 'react';
import { cn } from './cn';

interface TabListProps extends HTMLAttributes<HTMLDivElement> {
  compact?: boolean;
  children: ReactNode;
}

interface TabProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  active?: boolean;
  children: ReactNode;
}

export function TabList({ compact = false, className, children, ...props }: TabListProps) {
  return (
    <div
      role="tablist"
      className={cn('ui-tablist', compact && 'ui-tablist--compact', className)}
      {...props}
    >
      {children}
    </div>
  );
}

export function Tab({ active = false, className, children, ...props }: TabProps) {
  return (
    <button
      type="button"
      role="tab"
      className={cn('ui-tab', active && 'is-active', className)}
      aria-selected={active}
      tabIndex={active ? 0 : -1}
      {...props}
    >
      {children}
    </button>
  );
}
