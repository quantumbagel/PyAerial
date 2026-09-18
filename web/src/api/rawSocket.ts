import type { RawWsMessage } from './types';

export type RawSocketHandlers = {
  onMessage: (message: RawWsMessage) => void;
  onOpen?: () => void;
  onClose?: (code?: number, reason?: string) => void;
};

let ws: WebSocket | null = null;
let isClosed = false;
let policyRejected = false;
let backoff = 1000;
const handlersSet = new Set<RawSocketHandlers>();

function connect() {
  if (policyRejected) return;
  isClosed = false;
  if (ws) return;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${window.location.host}/ws/raw`);

  ws.onopen = () => {
    backoff = 1000;
    handlersSet.forEach((h) => h.onOpen?.());
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data) as RawWsMessage;
      if (data && data.type === 'ping') return;
      handlersSet.forEach((h) => h.onMessage(data));
    } catch (e) {
      console.error('[WS raw] Error processing message:', e);
    }
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

export function connectRawSocket(handlers: RawSocketHandlers): () => void {
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
