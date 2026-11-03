import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Alert } from '../api/types';
import { AlertListItem } from './AlertListItem';
import { AlertTimeline } from './AlertTimeline';
import { makeAlert } from '../test/fixtures';

const ended: Alert = makeAlert({
  alert_id: 'f1:rdu:low',
  flight_id: 'f1',
  icao: 'abc123',
  callsign: 'TEST1',
  zone: 'RDU',
  rule: 'low',
  active: false,
  activated_at: 1700000000,
  deactivated_at: 1700000300,
  ended_by: 'lost',
});

describe('alert end reasons', () => {
  it('shows why an episode ended in the drawer timeline', () => {
    render(<AlertTimeline alerts={[ended]} activeAlertId={null} onSelectAlert={() => {}} />);
    expect(screen.getByText('aircraft lost')).toBeInTheDocument();
  });

  it('shows it in the alert list, and nothing for episodes without a reason', () => {
    const { unmount } = render(
      <ul>
        <AlertListItem alert={ended} active={false} sortField="activated" onSelect={() => {}} />
      </ul>,
    );
    expect(screen.getByText('Ended: aircraft lost')).toBeInTheDocument();
    unmount();

    render(
      <ul>
        <AlertListItem
          alert={{ ...ended, ended_by: null }}
          active={false}
          sortField="activated"
          onSelect={() => {}}
        />
      </ul>,
    );
    expect(screen.queryByText(/^Ended:/)).toBeNull();
  });
});
