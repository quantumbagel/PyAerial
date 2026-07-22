import { cn } from './cn';

type SpinnerSize = 'md' | 'lg' | 'xl';

interface SpinnerProps {
  size?: SpinnerSize;
  className?: string;
  'aria-hidden'?: boolean;
}

export function Spinner({ size = 'md', className, ...props }: SpinnerProps) {
  return (
    <span
      className={cn(
        'ui-spinner',
        size === 'lg' && 'ui-spinner--lg',
        size === 'xl' && 'ui-spinner--xl',
        className,
      )}
      aria-hidden={props['aria-hidden'] ?? true}
    />
  );
}
