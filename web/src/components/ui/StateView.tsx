import type { ReactNode } from 'react';
import { Button } from './Button';
import { Spinner } from './Spinner';
import { cn } from './cn';

export type StateKind = 'loading' | 'empty' | 'error';

interface StateViewProps {
  kind?: StateKind;
  /** `li` inside lists, `div` elsewhere. */
  as?: 'li' | 'div';
  /** `fill` grows to the available space; `inline` is a compact left-aligned note. */
  layout?: 'fill' | 'inline';
  onRetry?: () => void;
  className?: string;
  children: ReactNode;
}

/** One component for every loading / empty / error placeholder. */
export function StateView({
  kind = 'empty',
  as: Tag = 'div',
  layout = 'fill',
  onRetry,
  className,
  children,
}: StateViewProps) {
  return (
    <Tag className={cn('ui-state', `ui-state--${kind}`, `ui-state--${layout}`, className)}>
      {kind === 'loading' && <Spinner size={layout === 'inline' ? 'md' : 'lg'} />}
      {/* The role sits on the text so an <li> keeps its list-item semantics. */}
      <span role={kind === 'error' ? 'alert' : kind === 'loading' ? 'status' : undefined}>
        {children}
      </span>
      {kind === 'error' && onRetry && (
        <Button variant="primary" size="md" onClick={onRetry}>
          Try again
        </Button>
      )}
    </Tag>
  );
}
