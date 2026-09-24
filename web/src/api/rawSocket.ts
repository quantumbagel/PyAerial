import { BeastParser } from '../utils/beast';
import type { RawFrame } from './types';

export type BeastSocketHandlers = {
  onFrames: (frames: RawFrame[]) => void;
  onOpen?: () => void;
  onClose?: (code?: number, reason?: string) => void;
};

let ws: WebSocket | null = null;
let isClosed = false;
let policyRejected = false;
let backoff = 1000;
const handlersSet = new Set<BeastSocketHandlers>();
const parser = new BeastParser();

function connect() {
  if (policyRejected) return;
  isClosed = false;
  if (ws) return;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${window.location.host}/ws/beast`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    backoff = 1000;
    handlersSet.forEach((h) => h.onOpen?.());
  };

  ws.onmessage = (event) => {
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
  };

  ws.onclose = (event) => {
    ws = null;
    handlersSet.forEach((h) => h.onClose?.(event.code, event.reason));
    if (event.code === 1008) {
      policyRejected = true;
      isClosed = true;
      return;
    }
    if (!isClosed) {
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 10000);
    }
  };

  ws.onerror = () => {
    ws?.close();
  };
}

export function connectRawSocket(handlers: BeastSocketHandlers): () => void {
  handlersSet.add(handlers);
  if (!ws) {
    connect();
  } else if (ws.readyState === WebSocket.OPEN) {
    handlers.onOpen?.();
  }
  return () => {
    handlersSet.delete(handlers);
  };
}
