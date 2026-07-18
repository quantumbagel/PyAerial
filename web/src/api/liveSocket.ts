import type { LiveMessage } from './types';

export type LiveSocketHandlers = {
  onMessage: (message: LiveMessage) => void;
  onOpen?: () => void;
  onClose?: () => void;
};

let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let isClosed = false;
let backoff = 1000;
const handlersSet = new Set<LiveSocketHandlers>();
const pendingRequests = new Map<string, { resolve: (val: any) => void; reject: (err: any) => void }>();
const requestQueue: { id: string; action: string; params: any; resolve: any; reject: any }[] = [];

function generateId(): string {
  return Math.random().toString(36).substring(2, 15);
}

function flushQueue() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  while (requestQueue.length > 0) {
    const req = requestQueue.shift();
    if (req) {
      pendingRequests.set(req.id, { resolve: req.resolve, reject: req.reject });
      ws.send(JSON.stringify({
        type: 'request',
        id: req.id,
        action: req.action,
        params: req.params
      }));
    }
  }
}

function connect() {
  if (isClosed || ws) return;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${window.location.host}/ws/live`);

  ws.onopen = () => {
    backoff = 1000;
    handlersSet.forEach((h) => h.onOpen?.());
    flushQueue();
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data && data.type === 'response') {
        const req = pendingRequests.get(data.id);
        if (req) {
          pendingRequests.delete(data.id);
          if (data.success) {
            req.resolve(data.data);
          } else {
            req.reject(new Error(data.error || 'Request failed'));
          }
        }
      } else {
        handlersSet.forEach((h) => h.onMessage(data as LiveMessage));
      }
    } catch {
      // ignore
    }
  };

  ws.onclose = () => {
    ws = null;
    handlersSet.forEach((h) => h.onClose?.());
    pendingRequests.forEach((req) => req.reject(new Error('Connection closed')));
    pendingRequests.clear();
    
    if (!isClosed) {
      reconnectTimer = setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 10000);
    }
  };

  ws.onerror = () => {
    ws?.close();
  };
}

export function subscribeLiveSocket(handlers: LiveSocketHandlers): () => void {
  handlersSet.add(handlers);
  if (!ws) {
    connect();
  } else if (ws.readyState === WebSocket.OPEN) {
    handlers.onOpen?.();
  }
  return () => {
    handlersSet.delete(handlers);
    if (handlersSet.size === 0) {
      isClosed = true;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      ws?.close();
      ws = null;
    }
  };
}

export function connectLiveSocket(handlers: LiveSocketHandlers): () => void {
  return subscribeLiveSocket(handlers);
}

export function sendWsRequest<T>(action: string, params: any = {}): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const id = generateId();
    if (ws && ws.readyState === WebSocket.OPEN) {
      pendingRequests.set(id, { resolve, reject });
      ws.send(JSON.stringify({
        type: 'request',
        id,
        action,
        params
      }));
    } else {
      requestQueue.push({ id, action, params, resolve, reject });
      if (!ws) {
        connect();
      }
    }
  });
}
