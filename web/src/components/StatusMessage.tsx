import type { ReactNode } from 'react';
import { Button, Spinner, cn } from './ui';

interface StatusMessageProps {
  children: ReactNode;
  variant?: 'loading' | 'empty' | 'error';
  onRetry?: () => void;
}

export function StatusMessage({ children, variant = 'empty', onRetry }: StatusMessageProps) {
  return (
    <li
      className={cn(
        'ui-empty',
        variant === 'loading' && 'ui-empty--loading',
        variant === 'error' && 'ui-empty--error',
      )}
    >
      {variant === 'loading' && <Spinner size="lg" />}
      <span>{children}</span>
      {variant === 'error' && onRetry && (
        <Button variant="primary" size="md" onClick={onRetry} style={{ marginTop: 'var(--space-2)' }}>
          Try again
        </Button>
      )}
    </li>
  );
}
