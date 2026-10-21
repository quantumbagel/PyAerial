import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ComponentProps } from 'react';
import { DetailsDrawer } from './DetailsDrawer';

type Props = ComponentProps<typeof DetailsDrawer>;

function baseProps(overrides: Partial<Props> = {}): Props {
  return {
    open: false,
    flightDetail: null,
    flightSummary: null,
    activeAlertId: null,
    flightAlerts: [],
    flightTelemetry: [],
    drawerTab: 'telemetry',
    selectedTelemetryPoint: null,
    appConfig: null,
    isLoading: true,
    onSelectTelemetryPoint: vi.fn(),
    onClose: vi.fn(),
    onSwitchTab: vi.fn(),
    onSelectAlert: vi.fn(),
    ...overrides,
  };
}

function Harness(props: Props) {
  return (
    <>
      <button type="button">opener</button>
      <DetailsDrawer {...props} />
    </>
  );
}

describe('DetailsDrawer focus', () => {
  it('focuses once on open, not on every re-render, and restores focus on close', () => {
    const { rerender } = render(<Harness {...baseProps()} />);
    const opener = screen.getByText('opener');
    opener.focus();

    rerender(<Harness {...baseProps({ open: true })} />);
    const drawer = screen.getByRole('complementary');
    expect(document.activeElement).toBe(drawer);

    const close = screen.getByRole('button', { name: /close/i });
    close.focus();
    // A parent re-render hands down a brand-new onClose; focus must stay put.
    rerender(<Harness {...baseProps({ open: true, onClose: vi.fn() })} />);
    expect(document.activeElement).toBe(close);

    rerender(<Harness {...baseProps({ open: false })} />);
    expect(document.activeElement).toBe(opener);
  });

  it('is a labelled complementary region, not a modal dialog', () => {
    render(<Harness {...baseProps({ open: true })} />);
    const drawer = screen.getByRole('complementary');
    expect(drawer).not.toHaveAttribute('aria-modal');
    expect(drawer).toHaveAttribute('aria-labelledby');
    expect(document.getElementById(drawer.getAttribute('aria-labelledby')!)?.tagName).toBe('H2');
  });
});

describe('DetailsDrawer Escape', () => {
  it('closes on Escape, but not when a child already handled it', () => {
    const onClose = vi.fn();
    render(<Harness {...baseProps({ open: true, onClose })} />);
    const handled = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
    handled.preventDefault();
    window.dispatchEvent(handled);
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
