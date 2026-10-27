import { useEffect, useState } from 'react';
import { connectRawSocket } from '../api/rawSocket';
import { POLICY_VIOLATION } from '../api/reconnectingSocket';
import type { BufferedRawFrame, WsStatus } from '../api/types';
import { RAW_BUFFER_CAP, prependRawFrames } from '../utils/rawFrames';

/** UI refresh rate for the raw frame list (frames are batched in between). */
export const RAW_FLUSH_MS = 250;

let seq = 0;

/**
 * JSON raw frames, newest first. The `/ws/raw` socket is only open while
 * `enabled` is true, and incoming frames are batched so React re-renders at
 * most every RAW_FLUSH_MS no matter how busy the receiver is.
 */
export function useRawFrames(enabled: boolean) {
  const [frames, setFrames] = useState<BufferedRawFrame[]>([]);
  const [status, setStatus] = useState<WsStatus>('connecting');

  useEffect(() => {
    if (!enabled) return undefined;
    let pending: BufferedRawFrame[] = [];
    const flush = () => {
      if (!pending.length) return;
      const batch = pending;
      pending = [];
      setFrames((prev) => prependRawFrames(prev, batch));
    };
    const timer = setInterval(flush, RAW_FLUSH_MS);
    setStatus('connecting');
    const disconnect = connectRawSocket({
      onOpen: () => setStatus('connected'),
      onClose: (code) => setStatus(code === POLICY_VIOLATION ? 'rejected' : 'reconnecting'),
      onFrames: (incoming) => {
        for (const row of incoming) {
          seq += 1;
          pending.push({ ...row, id: String(seq) });
        }
        // Only the newest RAW_BUFFER_CAP frames can ever be shown.
        if (pending.length > RAW_BUFFER_CAP) pending = pending.slice(-RAW_BUFFER_CAP);
      },
    });
    return () => {
      clearInterval(timer);
      disconnect();
    };
  }, [enabled]);

  return { frames, status };
}
