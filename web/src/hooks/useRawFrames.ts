import { useEffect, useState } from 'react';
import { connectRawSocket } from '../api/rawSocket';
import { POLICY_VIOLATION, TRY_AGAIN_LATER } from '../api/reconnectingSocket';
import type { BufferedRawFrame, WsStatus } from '../api/types';
import { RAW_BUFFER_CAP, prependRawFrames } from '../utils/rawFrames';

/** UI refresh rate for the raw frame list (frames are batched in between). */
export const RAW_FLUSH_MS = 250;

let seq = 0;

/** What a `/ws/raw` close means for the panel (it retries on its own unless rejected). */
export function rawCloseStatus(code?: number): WsStatus {
  if (code === POLICY_VIOLATION) return 'rejected';
  if (code === TRY_AGAIN_LATER) return 'busy';
  return 'reconnecting';
}

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
    const disconnect = connectRawSocket({
      onOpen: () => setStatus('connected'),
      onClose: (code) => setStatus(rawCloseStatus(code)),
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
      // The next subscription starts out connecting, not with this one's status.
      setStatus('connecting');
    };
  }, [enabled]);

  return { frames, status };
}
