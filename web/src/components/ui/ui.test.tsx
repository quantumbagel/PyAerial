import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SortControl } from './SortControl';
import { Tab, TabList, TabPanel, useTabIds } from './Tabs';

const fields = [
  { id: 'a', label: 'Alpha' },
  { id: 'b', label: 'Beta' },
  { id: 'c', label: 'Gamma' },
] as const;

function Sorter({ onChange = vi.fn() }: { onChange?: (v: string) => void }) {
  return (
    <SortControl
      noun="flights"
      fields={fields}
      value="b"
      direction="desc"
      onChange={onChange}
      onToggleDirection={vi.fn()}
    />
  );
}

describe('SortControl', () => {
  it('labels the native field select', () => {
    render(<Sorter />);
    const select = screen.getByRole('combobox', { name: /sort flights by/i });
    expect(select).toHaveValue('b');
    expect(screen.getByRole('option', { name: 'Beta' })).toBeInTheDocument();
  });

  it('changes field via the native select', () => {
    const onChange = vi.fn();
    render(<Sorter onChange={onChange} />);
    fireEvent.change(screen.getByRole('combobox', { name: /sort flights by/i }), {
      target: { value: 'a' },
    });
    expect(onChange).toHaveBeenCalledWith('a');
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
