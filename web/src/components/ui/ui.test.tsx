import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SortControl } from './SortControl';
import { Tab, TabList, TabPanel, useTabIds } from './Tabs';

const options = [
  { value: 'a', label: 'Alpha' },
  { value: 'b', label: 'Beta' },
  { value: 'c', label: 'Gamma' },
] as const;

function Sorter({ onChange = vi.fn() }: { onChange?: (v: string) => void }) {
  return (
    <SortControl
      noun="flights"
      options={[...options]}
      value="b"
      direction="desc"
      onChange={onChange}
      onToggleDirection={vi.fn()}
    />
  );
}

describe('SortControl', () => {
  it('has a labelled button and moves focus into the list on open', () => {
    render(<Sorter />);
    const button = screen.getByRole('button', { name: /sort flights by.*beta/i });
    fireEvent.click(button);
    const selected = screen.getByRole('option', { name: 'Beta' });
    expect(document.activeElement).toBe(selected);
    expect(selected).toHaveAttribute('tabindex', '0');
    expect(screen.getByRole('option', { name: 'Alpha' })).toHaveAttribute('tabindex', '-1');
  });

  it('moves a roving tabindex with arrows and picks with Enter', () => {
    const onChange = vi.fn();
    render(<Sorter onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: /sort flights by/i }));
    const list = screen.getByRole('listbox');
    fireEvent.keyDown(list, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(screen.getByRole('option', { name: 'Gamma' }));
    fireEvent.keyDown(list, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(screen.getByRole('option', { name: 'Alpha' }));
    fireEvent.keyDown(list, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith('a');
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('consumes Escape so the drawer does not also close', () => {
    const windowListener = vi.fn();
    window.addEventListener('keydown', windowListener);
    render(<Sorter />);
    const button = screen.getByRole('button', { name: /sort flights by/i });
    fireEvent.click(button);
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' });
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(document.activeElement).toBe(button);
    expect(windowListener).not.toHaveBeenCalled();
    window.removeEventListener('keydown', windowListener);
  });

  it('hides decorative arrows from assistive tech', () => {
    render(<Sorter />);
    expect(screen.getByRole('button', { name: 'Sort direction: descending' })).toBeInTheDocument();
    expect(screen.getByText('↓')).toHaveAttribute('aria-hidden', 'true');
  });
});

function TabSet({ label }: { label: string }) {
  const ids = useTabIds();
  return (
    <>
      <TabList aria-label={label}>
        <Tab id={ids.tab('alerts')} controls={ids.panel('alerts')} active>
          {label} alerts
        </Tab>
      </TabList>
      <TabPanel id={ids.panel('alerts')} labelledBy={ids.tab('alerts')} active>
        panel
      </TabPanel>
    </>
  );
}

describe('Tabs', () => {
  it('never produces duplicate ids across tab sets and wires aria-controls', () => {
    const { container } = render(
      <>
        <TabSet label="Sidebar" />
        <TabSet label="Drawer" />
      </>,
    );
    const ids = Array.from(container.querySelectorAll('[id]')).map((el) => el.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const tab of screen.getAllByRole('tab')) {
      const panel = document.getElementById(tab.getAttribute('aria-controls')!);
      expect(panel).toHaveAttribute('role', 'tabpanel');
      expect(panel).toHaveAttribute('aria-labelledby', tab.id);
    }
  });
});
