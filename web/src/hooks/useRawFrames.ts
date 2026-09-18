import { useEffect, useRef, useState } from 'react';
import { connectRawSocket } from '../api/rawSocket';
import type { BufferedRawFrame, RawAntenna, RawFrame, WsStatus } from '../api/types';
import { prependRawFrames } from '../utils/rawFrames';

export function useRawFrames() {
  const [frames, setFrames] = useState<BufferedRawFrame[]>([]);
  const [antenna, setAntenna] = useState<RawAntenna | null>(null);
  const [status, setStatus] = useState<WsStatus>('connecting');
  const seq = useRef(0);

  useEffect(() => {
    const disconnect = connectRawSocket({
      onOpen: () => setStatus('connected'),
      onClose: (code) => {
        setStatus(code === 1008 ? 'rejected' : 'reconnecting');
      },
      onMessage: (message) => {
        if (message.type === 'antenna') {
          setAntenna(message.antenna);
          return;
        }
        if (message.type !== 'raw' || !Array.isArray(message.messages)) return;
        const tagged: BufferedRawFrame[] = message.messages.map((row: RawFrame) => ({
          ...row,
          id: String(++seq.current),
        }));
        if (!tagged.length) return;
        setFrames((prev) => prependRawFrames(prev, tagged));
      },
    });
    return disconnect;
  }, []);

  return { frames, antenna, status };
}
