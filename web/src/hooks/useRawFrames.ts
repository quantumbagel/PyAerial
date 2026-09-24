import { useEffect, useRef, useState } from 'react';
import { connectRawSocket } from '../api/rawSocket';
import type { BufferedRawFrame, WsStatus } from '../api/types';
import { prependRawFrames } from '../utils/rawFrames';

export function useRawFrames() {
  const [frames, setFrames] = useState<BufferedRawFrame[]>([]);
  const [status, setStatus] = useState<WsStatus>('connecting');
  const seq = useRef(0);

  useEffect(() => {
    const disconnect = connectRawSocket({
      onOpen: () => setStatus('connected'),
      onClose: (code) => {
        setStatus(code === 1008 ? 'rejected' : 'reconnecting');
      },
      onFrames: (incoming) => {
        const tagged: BufferedRawFrame[] = incoming.map((row) => ({
          ...row,
          id: String(++seq.current),
        }));
        if (!tagged.length) return;
        setFrames((prev) => prependRawFrames(prev, tagged));
      },
    });
    return disconnect;
  }, []);

  return { frames, status };
}
