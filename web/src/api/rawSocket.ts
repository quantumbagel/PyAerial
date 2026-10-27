import { ReconnectingSocket, socketUrl } from './reconnectingSocket';
import type { RawFrame } from './types';

export type RawSocketHandlers = {
  onFrames: (frames: RawFrame[]) => void;
  onOpen?: () => void;
  onClose?: (code?: number, reason?: string) => void;
};

const handlersSet = new Set<RawSocketHandlers>();

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function asRawFrame(item: unknown): RawFrame | null {
  if (!item || typeof item !== 'object') return null;
  const row = item as Record<string, unknown>;
  if (typeof row.hex !== 'string' || !row.hex || !isFiniteNumber(row.timestamp)) {
    return null;
  }
  const frame: RawFrame = { hex: row.hex, timestamp: row.timestamp };
  if (typeof row.receiver === 'string') frame.receiver = row.receiver;
  if (Array.isArray(row.receivers)) {
    frame.receivers = row.receivers.filter((name): name is string => typeof name === 'string');
  }
  if (isFiniteNumber(row.rssi)) frame.rssi = row.rssi;
  if (isFiniteNumber(row.clock)) frame.clock = row.clock;
  if (isFiniteNumber(row.df)) frame.df = row.df;
  if (typeof row.icao === 'string') frame.icao = row.icao;
  return frame;
}

/** Parse one `/ws/raw` text frame into engine-stamped RawFrame rows. */
export function parseRawMessage(data: unknown): RawFrame[] {
  if (typeof data !== 'string') return [];
  try {
    const parsed = JSON.parse(data) as { type?: unknown; frames?: unknown };
    if (parsed?.type !== 'raw' || !Array.isArray(parsed.frames)) return [];
    const frames: RawFrame[] = [];
    for (const item of parsed.frames) {
      const frame = asRawFrame(item);
      if (frame) frames.push(frame);
    }
    return frames;
  } catch {
    return [];
  }
}

// Quiet receivers are legitimate, so there is no idle timeout here.
const socket = new ReconnectingSocket({
  url: () => socketUrl('/ws/raw'),
  onOpen: () => {
    handlersSet.forEach((h) => h.onOpen?.());
  },
  onMessage: (event) => {
    const frames = parseRawMessage(event.data);
    if (!frames.length) return;
    handlersSet.forEach((h) => h.onFrames(frames));
  },
  onClose: (code, reason) => {
    handlersSet.forEach((h) => h.onClose?.(code, reason));
  },
});

/**
 * Subscribe to JSON raw frames. The socket is only open while at least
 * one subscriber exists, so closing the Raw panel stops the stream.
 */
export function connectRawSocket(handlers: RawSocketHandlers): () => void {
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
    }
  };
}
