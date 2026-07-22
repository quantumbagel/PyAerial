import type { HTMLAttributes, ReactNode } from 'react';
import { cn } from './cn';

interface ChipProps extends HTMLAttributes<HTMLSpanElement> {
  children: ReactNode;
}

export function Chip({ className, children, ...props }: ChipProps) {
  return (
    <span className={cn('ui-chip', className)} {...props}>
      {children}
    </span>
  );
}
