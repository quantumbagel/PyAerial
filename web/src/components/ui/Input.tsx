import type { InputHTMLAttributes } from 'react';
import { cn } from './cn';

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  search?: boolean;
}

export function Input({ search = false, className, ...props }: InputProps) {
  return (
    <input
      className={cn('ui-input', search && 'ui-input--search', className)}
      {...props}
    />
  );
}
