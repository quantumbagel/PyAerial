import type { ReactNode } from 'react';
import { cn } from './cn';

interface StatProps {
  className?: string;
  title?: string;
  children: ReactNode;
}

interface StatValueProps {
  tone?: 'live' | 'warn' | 'alert';
  className?: string;
  id?: string;
  children: ReactNode;
}

export function Stat({ className, title, children }: StatProps) {
  return (
    <div className={cn('ui-stat', className)} title={title}>
      {children}
    </div>
  );
}

export function StatValue({ tone, className, id, children }: StatValueProps) {
  return (
    <strong
      id={id}
      className={cn(tone && `ui-stat__value--${tone}`, className)}
    >
      {children}
    </strong>
  );
}
