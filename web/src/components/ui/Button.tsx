import type { AnchorHTMLAttributes, ButtonHTMLAttributes, ReactNode, Ref } from 'react';
import { cn } from './cn';

type ButtonVariant = 'primary' | 'ghost' | 'toggle' | 'icon' | 'subtle' | 'link';
type ButtonSize = 'sm' | 'md';

type SharedProps = {
  variant?: ButtonVariant;
  size?: ButtonSize;
  active?: boolean;
  flex?: boolean;
  zoom?: boolean;
  className?: string;
  children: ReactNode;
};

type ButtonProps = SharedProps &
  ButtonHTMLAttributes<HTMLButtonElement> & {
    as?: 'button';
    ref?: Ref<HTMLButtonElement>;
  };

type LinkButtonProps = SharedProps &
  AnchorHTMLAttributes<HTMLAnchorElement> & {
    as: 'a';
    ref?: Ref<HTMLAnchorElement>;
  };

export function Button({
  variant = 'toggle',
  size = 'sm',
  active = false,
  flex = false,
  zoom = false,
  className,
  children,
  as,
  ...props
}: ButtonProps | LinkButtonProps) {
  const skipSize = variant === 'ghost' || variant === 'link' || variant === 'icon';
  const classes = cn(
    'ui-btn',
    `ui-btn--${variant}`,
    !skipSize && `ui-btn--${size}`,
    active && 'is-active',
    flex && 'ui-btn--flex',
    zoom && 'ui-btn--zoom',
    className,
  );

  if (as === 'a') {
    const anchorProps = props as AnchorHTMLAttributes<HTMLAnchorElement> & {
      ref?: Ref<HTMLAnchorElement>;
    };
    return (
      <a className={classes} {...anchorProps}>
        {children}
      </a>
    );
  }

  const buttonProps = props as ButtonHTMLAttributes<HTMLButtonElement> & {
    ref?: Ref<HTMLButtonElement>;
  };
  return (
    <button type="button" className={classes} {...buttonProps}>
      {children}
    </button>
  );
}
