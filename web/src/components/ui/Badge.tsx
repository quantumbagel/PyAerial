import type { CSSProperties, ReactNode } from 'react';
import { cn } from './cn';

type BadgeVariant = 'live' | 'warn' | 'alert' | 'info' | 'neutral' | 'zone';

interface BadgeProps {
  variant?: BadgeVariant;
  className?: string;
  style?: CSSProperties;
  children: ReactNode;
}

export function Badge({ variant = 'neutral', className, style, children }: BadgeProps) {
  return (
    <span className={cn('ui-badge', `ui-badge--${variant}`, className)} style={style}>
      {children}
    </span>
  );
}

export function BadgeGroup({ className, children }: { className?: string; children: ReactNode }) {
  return <span className={cn('ui-badge-group', className)}>{children}</span>;
}
