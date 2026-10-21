import { useMemo, useSyncExternalStore } from 'react';
import type { FlightSummary, RawAntenna, ServerStats } from '../api/types';
import { useRawFrames } from '../hooks/useRawFrames';
import { emptyRawMessage, filterRawFrames } from '../utils/rawFrames';
import { RawListItem } from './RawListItem';
import { StatusMessage } from './StatusMessage';
import { Chip } from './ui';

function subscribeVisibility(onChange: () => void) {
  document.addEventListener('visibilitychange', onChange);
  return () => document.removeEventListener('visibilitychange', onChange);
}

function usePageVisible(): boolean {
  return useSyncExternalStore(
    subscribeVisibility,
    () => document.visibilityState !== 'hidden',
    () => true,
  );
}

interface RawPanelProps {
  searchQuery: string;
  flights: FlightSummary[];
  antenna: RawAntenna | null;
  stats: ServerStats | null;
  onSelectIcao?: (icao: string) => void;
}

/**
 * The Raw tab body. Mounted only while the tab is shown; owns the Beast feed so
 * the rest of the app never re-renders for raw frames.
 */
export function RawPanel({ searchQuery, flights, antenna, stats, onSelectIcao }: RawPanelProps) {
  const visible = usePageVisible();
  const { frames, status } = useRawFrames(visible);
  const filtered = useMemo(() => filterRawFrames(frames, searchQuery), [frames, searchQuery]);
  const selectableIcaos = useMemo(() => {
    const set = new Set<string>();
    for (const flight of flights) {
      if (flight.icao) set.add(flight.icao.toLowerCase());
    }
    return set;
  }, [flights]);

  return (
    <>
      {antenna?.receivers?.length ? (
        <div className="raw-antenna" aria-label="Configured receivers">
          {antenna.receivers.map((receiver) => (
            <Chip key={receiver.name}>
              {receiver.name}
              {receiver.format ? ` · ${receiver.format}` : ` · ${receiver.type}`}
            </Chip>
          ))}
        </div>
      ) : null}
      <ul id="raw-list">
        {filtered.length === 0 && (status === 'connecting' || status === 'reconnecting') ? (
          <StatusMessage variant="loading">
            {status === 'reconnecting' ? 'Reconnecting to Beast stream…' : 'Connecting to Beast stream…'}
          </StatusMessage>
        ) : filtered.length === 0 ? (
          <StatusMessage>
            {emptyRawMessage({ hasFilters: Boolean(searchQuery), stats, rawStatus: status })}
          </StatusMessage>
        ) : (
          filtered.map((row) => (
            <RawListItem
              key={row.id}
              frame={row}
              selectable={Boolean(
                row.icao && onSelectIcao && selectableIcaos.has(row.icao.toLowerCase()),
              )}
              onSelectIcao={onSelectIcao}
            />
          ))
        )}
      </ul>
    </>
  );
}
