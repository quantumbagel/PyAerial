import type { LiveMessage } from './types';

export type LiveSocketHandlers = {
  onMessage: (message: LiveMessage) => void;
  onOpen?: () => void;
  onClose?: () => void;
};

export function connectLiveSocket(handlers: LiveSocketHandlers): () => void {
  let ws: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;
  let backoff = 1000;

  const connect = () => {
    if (closed) return;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${window.location.host}/ws/live`);

    ws.onopen = () => {
      backoff = 1000;
      handlers.onOpen?.();
    };

    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data) as LiveMessage;
        handlers.onMessage(message);
      } catch {
        // ignore malformed messages
      }
    };

    ws.onclose = () => {
      handlers.onClose?.();
      if (!closed) {
        reconnectTimer = setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 10000);
      }
    };

    ws.onerror = () => {
      ws?.close();
    };
  };

  connect();

  return () => {
    closed = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    ws?.close();
  };
}
