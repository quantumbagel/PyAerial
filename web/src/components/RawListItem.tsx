import { memo } from 'react';
import type { BufferedRawFrame } from '../api/types';
import { EMPTY, LIST_SEP, displayIcao, formatTime } from '../utils/format';
import { formatRawDf, formatRawRssi } from '../utils/rawFrames';
import { Chip } from './ui';

interface RawListItemProps {
  frame: BufferedRawFrame;
  selectable: boolean;
  onSelectIcao?: (icao: string) => void;
}

export const RawListItem = memo(function RawListItem({ frame, selectable, onSelectIcao }: RawListItemProps) {
  const icao = frame.icao ? displayIcao(frame.icao) : null;
  const rssi = formatRawRssi(frame.rssi);
  const time = formatTime(frame.timestamp, { withSeconds: true });
  const source = frame.receiver || (frame.clock != null ? `clock ${frame.clock}` : null);
  const title = [icao ?? 'No ICAO', formatRawDf(frame.df), source, rssi, frame.hex]
    .filter(Boolean)
    .join(LIST_SEP);

  const body = (
    <>
      <div className="ui-row__line">
        <span className="ui-row__title">
          {icao ?? EMPTY}
          <Chip>{formatRawDf(frame.df)}</Chip>
        </span>
        <span className="ui-row__trailing">{time}</span>
      </div>
      <div className="ui-row__line">
        <span className="ui-row__secondary raw-hex">{frame.hex}</span>
        <span className="ui-row__trailing">{[source, rssi].filter(Boolean).join(LIST_SEP) || EMPTY}</span>
      </div>
    </>
  );

  if (selectable && icao && onSelectIcao) {
    return (
      <li>
        <button type="button" className="ui-row" title={title} onClick={() => onSelectIcao(icao)}>
          {body}
        </button>
      </li>
    );
  }

  return (
    <li>
      <div className="ui-row ui-row--static ui-row--muted" title={title}>
        {body}
      </div>
    </li>
  );
});
