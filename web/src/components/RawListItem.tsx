import type { BufferedRawFrame } from '../api/types';
import { formatTime } from '../utils/format';
import { formatRawDf, formatRawRssi } from '../utils/rawFrames';
import { Chip } from './ui';

interface RawListItemProps {
  frame: BufferedRawFrame;
  selectable: boolean;
  onSelectIcao?: (icao: string) => void;
}

export function RawListItem({ frame, selectable, onSelectIcao }: RawListItemProps) {
  const icao = (frame.icao || '').toUpperCase();
  const rssi = formatRawRssi(frame.rssi);
  const title = [
    icao || 'No ICAO',
    formatRawDf(frame.df),
    frame.receiver || (frame.clock != null ? `clock ${frame.clock}` : ''),
    rssi,
    frame.hex,
    frame.clock != null ? `clock ${frame.clock}` : '',
  ]
    .filter(Boolean)
    .join(' · ');

  const body = (
    <>
      <div className="ui-row__line">
        <span className="ui-row__title">
          {icao || '—'}
          <Chip>{formatRawDf(frame.df)}</Chip>
        </span>
        <span className="ui-row__trailing">
          {rssi || formatTime(frame.timestamp, { withSeconds: true })}
        </span>
      </div>
      <div className="ui-row__line">
        <span className="ui-row__secondary raw-hex" title={frame.hex}>
          {frame.hex}
        </span>
        <span className="ui-row__trailing">
          {frame.receiver || (frame.clock != null ? `clk ${frame.clock}` : '—')}
          {rssi ? ` · ${formatTime(frame.timestamp, { withSeconds: true })}` : ''}
        </span>
      </div>
    </>
  );

  if (selectable && icao && onSelectIcao) {
    return (
      <li>
        <button
          type="button"
          className="ui-row"
          title={title}
          onClick={() => onSelectIcao(icao)}
        >
          {body}
        </button>
      </li>
    );
  }

  return (
    <li>
      <div className="ui-row ui-row--muted" title={title}>
        {body}
      </div>
    </li>
  );
}
