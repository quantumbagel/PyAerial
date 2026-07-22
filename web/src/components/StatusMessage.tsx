import type { ReactNode } from 'react';

interface StatusMessageProps {
  children: ReactNode;
  variant?: 'loading' | 'empty' | 'error';
  onRetry?: () => void;
}

export function StatusMessage({ children, variant = 'empty', onRetry }: StatusMessageProps) {
  return (
    <li className={`status-message status-message--${variant}`}>
      {variant === 'loading' && <span className="flight-list-spinner" aria-hidden="true" />}
      <span className="status-message-text">{children}</span>
      {variant === 'error' && onRetry && (
        <button type="button" className="btn-try-again" onClick={onRetry}>
          Try again
        </button>
      )}
    </li>
  );
}
