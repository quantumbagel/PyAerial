import { BeastParser } from '../utils/beast';
import { ReconnectingSocket, socketUrl } from './reconnectingSocket';
import type { RawFrame } from './types';

export type BeastSocketHandlers = {
  onFrames: (frames: RawFrame[]) => void;
  onOpen?: () => void;
  onClose?: (code?: number, reason?: string) => void;
};

const handlersSet = new Set<BeastSocketHandlers>();
const parser = new BeastParser();

// The Beast stream is raw bytes with no keepalive, and a quiet receiver is
// legitimate, so there is no idle timeout here.
const socket = new ReconnectingSocket({
  url: () => socketUrl('/ws/beast'),
  binaryType: 'arraybuffer',
  onOpen: () => {
    parser.reset();
    handlersSet.forEach((h) => h.onOpen?.());
  },
  onMessage: (event) => {
    if (!(event.data instanceof ArrayBuffer)) return;
    const decoded = parser.feed(new Uint8Array(event.data));
    if (!decoded.length) return;
    const now = Date.now() / 1000;
    const frames: RawFrame[] = decoded.map((frame) => ({
      hex: frame.hex,
      timestamp: now,
      rssi: frame.rssi,
      clock: frame.clock,
      df: frame.df,
      icao: frame.icao,
    }));
    handlersSet.forEach((h) => h.onFrames(frames));
  },
  onClose: (code, reason) => {
    parser.reset();
    handlersSet.forEach((h) => h.onClose?.(code, reason));
  },
});

/**
 * Subscribe to decoded Beast frames. The socket is only open while at least
 * one subscriber exists, so closing the Raw panel stops the byte stream.
 */
export function connectRawSocket(handlers: BeastSocketHandlers): () => void {
  handlersSet.add(handlers);
  if (socket.isReady) {
    handlers.onOpen?.();
  } else if (!socket.isRunning) {
    socket.start();
  }
  return () => {
    handlersSet.delete(handlers);
    if (handlersSet.size === 0) {
      socket.stop();
      parser.reset();
    }
  };
}
